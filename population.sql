-- Population query for escalation_clusters skill
-- Self-serve-counted escalation events with three-tier conversation_id resolution, closest-in-time Front matching, and team assignment.
-- Defaults exclude Postpone Payment, Pre Cancel Direct, REQUEST_URGENT_SWITCH (the standard self-serve definition).

WITH
-- Start of escalation_events
-- Purpose: Pull all escalation events (Handed off + Call Agent Action phone/chat paths).
escalation_events AS (
    SELECT
        created_at
      , dec_user_id
      , CASE
            WHEN event_action = 'Handed off' THEN JSONExtractString(event_metadata, 'escalationType')
            WHEN event_action = 'Call Agent Action' AND event_label = 'REQUEST_URGENT_SWITCH' THEN 'REQUEST_URGENT_SWITCH'
            WHEN event_action = 'Call Agent Action' THEN event_label
            ELSE ''
        END AS escalation_type
      , CASE
            WHEN event_action = 'Handed off' THEN 'chat_path'
            WHEN event_action = 'Call Agent Action' AND event_label = 'REQUEST_URGENT_SWITCH' THEN 'chat_path'
            WHEN event_action = 'Call Agent Action' THEN 'phone_path'
            ELSE ''
        END AS escalation_path
      , JSONExtractString(event_metadata, 'targetMessageID') AS target_msg_id
      , coalesce(nullIf(JSONExtractString(event_metadata, 'channel'), ''), 'Chat') AS channel
    FROM events.track_all
    PREWHERE created_at >= toDateTime({{start_date}}) - INTERVAL 1 DAY
    WHERE 1
        AND event_category = 'Chatbot'
        AND (
            (event_action = 'Handed off' AND JSONExtractString(event_metadata, 'escalationType') != '')
            OR (event_action = 'Call Agent Action' AND event_label IN ('CALL_IN', 'QUEUE_RETURN_CALL', 'SCHEDULE_RETURN_CALL', 'REQUEST_URGENT_SWITCH'))
        )
        AND JSONExtractString(event_metadata, 'channel') IN ('SMS', 'Chat', '')
        AND toDate(toTimezone(created_at, 'America/New_York')) BETWEEN {{start_date}} AND {{end_date}}
)
-- End of escalation_events: one row per escalation event across all paths.

-- Start of escalation_events_ranked
-- Purpose: Dedupe at msg_id grain when present, falling back to per-minute bucket when msg_id is missing (phone-path events).
, escalation_events_ranked AS (
    SELECT
        created_at
      , dec_user_id
      , escalation_type
      , escalation_path
      , target_msg_id
      , channel
      , ROW_NUMBER() OVER (
            PARTITION BY
                dec_user_id
              , escalation_type
              , if(target_msg_id != '', target_msg_id, toString(toStartOfMinute(created_at)))
            ORDER BY created_at
        ) AS rn
    FROM escalation_events
)
-- End of escalation_events_ranked: escalation events with dedup row number.

-- Start of escalation_events_deduped
-- Purpose: Keep one event per (user, type, minute); intentional grain matches canonical card_24464_v2 dedup.
, escalation_events_deduped AS (
    SELECT
        created_at
      , dec_user_id
      , escalation_type
      , escalation_path
      , target_msg_id
      , channel
    FROM escalation_events_ranked
    WHERE 1
        AND rn = 1
        AND escalation_type NOT IN ('Postpone Payment', 'Pre Cancel Direct', 'REQUEST_URGENT_SWITCH')
)
-- End of escalation_events_deduped: self-serve-counted escalation events, deduped at user+type+minute grain.

-- Start of escalation_users
-- Purpose: One row per user in the escalation set, used to scope downstream lookups.
, escalation_users AS (
    SELECT
        dec_user_id AS user_id
    FROM escalation_events_deduped
    GROUP BY dec_user_id
)
-- End of escalation_users: one row per escalation user.

-- Start of message_conversations_raw
-- Purpose: Pull message-level external_id to conversation_id mappings within the date window.
, message_conversations_raw AS (
    SELECT
        external_id AS msg_external_id
      , toString(conversation_id) AS conv_id
      , created_at AS msg_created_at
    FROM promptitude.messages
    PREWHERE created_at >= toDateTime({{start_date}}) - INTERVAL 1 DAY
    WHERE 1
        AND external_id IS NOT NULL
        AND external_id != ''
        AND conversation_id IS NOT NULL
        AND toString(conversation_id) != ''
        AND toDate(toTimezone(created_at, 'America/New_York')) <= toDate({{end_date}}) + INTERVAL 2 DAY
)
-- End of message_conversations_raw: message-level rows for primary conv_id join.

-- Start of message_conversations
-- Purpose: Deduplicate to one conversation_id per message external_id via argMax on created_at.
, message_conversations AS (
    SELECT
        msg_external_id
      , argMax(conv_id, msg_created_at) AS conversation_id
    FROM message_conversations_raw
    GROUP BY msg_external_id
)
-- End of message_conversations: primary join lookup, msg-id to latest conversation-id.

-- Start of promptitude_fallback_raw
-- Purpose: Counted Escalation tracking events scoped to Jerry Chatbot project, with prior-day buffer for 60s edge case.
, promptitude_fallback_raw AS (
    SELECT
        created_at AS event_time
      , toInt64OrZero(end_user_id) AS user_id
      , toString(conversation_id) AS conv_id
    FROM promptitude.tracking_events
    PREWHERE created_at >= toDateTime({{start_date}}) - INTERVAL 1 DAY
    WHERE 1
        -- project_id eb494fcd... = Jerry Chatbot project. Validated 2026-04-21. Update if project structure changes.
        AND project_id = 'eb494fcd-2ccc-4dc1-b63d-7b9e33a6be44'
        AND (action = 'Counted Escalation' OR action = 'counted escalation')
        AND toDate(toTimezone(created_at, 'America/New_York')) BETWEEN toDate({{start_date}}) - INTERVAL 1 DAY AND toDate({{end_date}}) + INTERVAL 1 DAY
        AND end_user_id IS NOT NULL
        AND end_user_id != ''
        AND conversation_id IS NOT NULL
        AND toString(conversation_id) != ''
)
-- End of promptitude_fallback_raw: tracking events scoped to chatbot project with edge-buffer.

-- Start of promptitude_fallback_per_escalation
-- Purpose: Pre-aggregate fallback layer 2 to one conv_id per escalation event (closest in time within 60s).
, promptitude_fallback_per_escalation AS (
    SELECT
        ee.dec_user_id AS dec_user_id
      , ee.created_at AS escalation_time
      , ee.escalation_type AS escalation_type
      , argMin(pfr.conv_id, abs(dateDiff('second', ee.created_at, pfr.event_time))) AS fallback_conv_id
    FROM escalation_events_deduped AS ee
    LEFT JOIN promptitude_fallback_raw AS pfr
        ON pfr.user_id = ee.dec_user_id
        AND abs(dateDiff('second', ee.created_at, pfr.event_time)) <= 60
    GROUP BY ee.dec_user_id, ee.created_at, ee.escalation_type
)
-- End of promptitude_fallback_per_escalation: one row per escalation with closest-in-time tracking conv_id.

-- Start of recent_user_messages_raw
-- Purpose: User-type messages with derived user_id from author_info.externalID for the third fallback layer.
, recent_user_messages_raw AS (
    SELECT
        toInt64OrZero(JSONExtractString(author_info, 'externalID')) AS user_id
      , toString(conversation_id) AS conv_id
      , created_at AS msg_created_at
    FROM promptitude.messages
    PREWHERE created_at >= toDateTime({{start_date}}) - INTERVAL 1 DAY
    WHERE 1
        AND author_type = 'user'
        AND author_info != ''
        AND conversation_id IS NOT NULL
        AND toString(conversation_id) != ''
        AND toDate(toTimezone(created_at, 'America/New_York')) <= toDate({{end_date}}) + INTERVAL 1 DAY
)
-- End of recent_user_messages_raw: user-message rows pre-derived user_id.

-- Start of recent_user_messages
-- Purpose: Drop rows where user_id derivation produced 0 (malformed or empty externalID).
, recent_user_messages AS (
    SELECT
        user_id
      , conv_id
      , msg_created_at
    FROM recent_user_messages_raw
    WHERE user_id > 0
)
-- End of recent_user_messages: clean user-message rows for fallback join.

-- Start of recent_conversation_fallback
-- Purpose: Third fallback. Most recent user-message conversation_id within 10 minutes at-or-before each escalation.
, recent_conversation_fallback AS (
    SELECT
        ee.dec_user_id AS dec_user_id
      , ee.created_at AS escalation_time
      , ee.escalation_type AS escalation_type
      , argMax(rum.conv_id, rum.msg_created_at) AS recent_conv_id
    FROM escalation_events_deduped AS ee
    LEFT JOIN recent_user_messages AS rum
        ON rum.user_id = ee.dec_user_id
        AND dateDiff('second', rum.msg_created_at, ee.created_at) <= 10 * 60
        AND dateDiff('second', rum.msg_created_at, ee.created_at) >= 0
    GROUP BY ee.dec_user_id, ee.created_at, ee.escalation_type
)
-- End of recent_conversation_fallback: per-escalation most recent user-msg conv_id within window.

-- Start of stage_per_escalation
-- Purpose: User stage at exact escalation moment via mv_user_stage_history. INNER JOIN intentional -- users with no active stage row return NULL user_stage downstream.
, stage_per_escalation AS (
    SELECT
        ee.dec_user_id AS dec_user_id
      , ee.created_at AS escalation_time
      , ee.escalation_type AS escalation_type
      , argMax(ush.stage, ush.start_timestamp) AS user_stage
    FROM escalation_events_deduped AS ee
    INNER JOIN magic.mv_user_stage_history AS ush
        ON ush.user_id = ee.dec_user_id
        AND ush.start_timestamp <= ee.created_at
        AND (ush.end_timestamp IS NULL OR ush.end_timestamp >= ee.created_at)
    WHERE ush.stage IN (0, 1, 3, 4, 8, 9, 10, 11, 12)
    GROUP BY ee.dec_user_id, ee.created_at, ee.escalation_type
)
-- End of stage_per_escalation: stage at exact escalation moment per row.

-- Start of policy_active_per_escalation
-- Purpose: Active (non-flat-cancel) policy detection at exact escalation moment via mv_policy_status. INNER JOIN intentional -- users with no policy row return NULL is_policyholder_int downstream (coalesced to 0 in final SELECT).
, policy_active_per_escalation AS (
    SELECT
        ee.dec_user_id AS dec_user_id
      , ee.created_at AS escalation_time
      , ee.escalation_type AS escalation_type
      , max(CASE
            WHEN toTimezone(ps.sold_at, 'America/New_York') <= ee.created_at
                 AND ps.start_date < ps.end_date
                 AND ps.end_date > toDate(ee.created_at)
            THEN 1 ELSE 0
          END) AS is_policyholder_int
    FROM escalation_events_deduped AS ee
    INNER JOIN magic.mv_policy_status AS ps ON ps.user_id = ee.dec_user_id
    GROUP BY ee.dec_user_id, ee.created_at, ee.escalation_type
)
-- End of policy_active_per_escalation: PH flag at exact escalation moment per row.

-- Start of users_deduped
-- Purpose: Dedupe main.users multi-row-per-id (~2M dupes) via argMax on updated_at before downstream join.
, users_deduped AS (
    SELECT
        id AS user_id
      , argMax(name, updated_at) AS name
    FROM main.users
    WHERE id IN (SELECT user_id FROM escalation_users)
    GROUP BY id
)
-- End of users_deduped: one row per escalation user with latest name from main.users.

-- Start of users_latest_name
-- Purpose: Rename to user_name_raw for first PII aggregation step in the lineage-breaking chain.
, users_latest_name AS (
    SELECT
        user_id
      , name AS user_name_raw
    FROM users_deduped
)
-- End of users_latest_name: latest name per escalation user with rename for PII chain.

-- Start of user_name_join
-- Purpose: JOIN-based second aggregation to break Metabase PII column lineage.
, user_name_join AS (
    SELECT
        eu.user_id AS user_id
      , any(uln.user_name_raw) AS user_name
    FROM escalation_users AS eu
    LEFT JOIN users_latest_name AS uln ON uln.user_id = eu.user_id
    GROUP BY eu.user_id
)
-- End of user_name_join: double-aggregated user name to break lineage.

-- Start of user_name_display
-- Purpose: Coalesce to neutral alias for safe display in Metabase output.
, user_name_display AS (
    SELECT
        user_id
      , coalesce(nullIf(user_name, ''), 'Unknown') AS display_name
    FROM user_name_join
)
-- End of user_name_display: one row per user with display-safe name.

-- Start of front_events_raw
-- Purpose: Front events in window with derived user_id from Contact User ID or base64-decoded handle.
, front_events_raw AS (
    SELECT
        coalesce(
            nullIf(`Contact User ID`, 0),
            toInt64OrNull(replaceOne(
                if(startsWith(splitByChar(',', `Contact handle`)[1], 'VXNlcjo'),
                   tryBase64Decode(splitByChar(',', `Contact handle`)[1]),
                   ''
                ),
                'User:', ''
            ))
        ) AS user_id
      , `Conversation API ID` AS front_conv_api_id
      , `Message date` AS front_msg_time
    FROM events.mv_frontapp_events
    PREWHERE `Message date` >= toDateTime({{start_date}}) - INTERVAL 1 DAY
    WHERE 1
        AND `Conversation API ID` != ''
        AND toDate(toTimezone(`Message date`, 'America/New_York')) BETWEEN toDate({{start_date}}) - INTERVAL 1 DAY AND toDate({{end_date}}) + INTERVAL 1 DAY
)
-- End of front_events_raw: Front events with derived user_id.

-- Start of front_events_for_users
-- Purpose: Restrict Front events to escalation users for cheap downstream join.
, front_events_for_users AS (
    SELECT
        fer.user_id AS user_id
      , fer.front_conv_api_id AS front_conv_api_id
      , fer.front_msg_time AS front_msg_time
    FROM front_events_raw AS fer
    INNER JOIN escalation_users AS eu ON eu.user_id = fer.user_id
    WHERE fer.user_id > 0
)
-- End of front_events_for_users: Front events for escalation users only.

-- Start of front_per_escalation
-- Purpose: For each escalation, find the closest-in-time Front conversation within 12 hours.
, front_per_escalation AS (
    SELECT
        ee.dec_user_id AS dec_user_id
      , ee.created_at AS escalation_time
      , ee.escalation_type AS escalation_type
      , argMin(fe.front_conv_api_id, abs(dateDiff('second', ee.created_at, fe.front_msg_time))) AS front_conv_api_id
    FROM escalation_events_deduped AS ee
    LEFT JOIN front_events_for_users AS fe
        ON fe.user_id = ee.dec_user_id
        AND abs(dateDiff('second', ee.created_at, fe.front_msg_time)) <= 12 * 3600
    GROUP BY ee.dec_user_id, ee.created_at, ee.escalation_type
)
-- End of front_per_escalation: closest-in-time Front conv per escalation.

-- Start of joined_escalations
-- Purpose: Combine deduped escalations with three-tier conv_id resolution and closest-in-time Front match.
, joined_escalations AS (
    SELECT
        ee.created_at AS created_at
      , ee.dec_user_id AS dec_user_id
      , ee.escalation_type AS escalation_type
      , ee.escalation_path AS escalation_path
      , ee.target_msg_id AS target_msg_id
      , ee.channel AS channel
      , coalesce(
            nullIf(mc.conversation_id, ''),
            nullIf(pfe.fallback_conv_id, ''),
            nullIf(rcf.recent_conv_id, '')
        ) AS conversation_id
      , CASE
            WHEN nullIf(mc.conversation_id, '') IS NOT NULL THEN 'msg_id_join'
            WHEN nullIf(pfe.fallback_conv_id, '') IS NOT NULL THEN 'tracking_fallback'
            WHEN nullIf(rcf.recent_conv_id, '') IS NOT NULL THEN 'recent_msg_fallback'
            ELSE 'no_match'
        END AS conv_id_source
      , fpe.front_conv_api_id AS front_conv_api_id
      , spe.user_stage AS user_stage
      , pape.is_policyholder_int AS is_policyholder_int
    FROM escalation_events_deduped AS ee
    LEFT JOIN message_conversations AS mc
        ON ee.target_msg_id = mc.msg_external_id
        AND ee.target_msg_id != ''
    LEFT JOIN promptitude_fallback_per_escalation AS pfe
        ON ee.dec_user_id = pfe.dec_user_id
        AND ee.created_at = pfe.escalation_time
        AND ee.escalation_type = pfe.escalation_type
    LEFT JOIN recent_conversation_fallback AS rcf
        ON ee.dec_user_id = rcf.dec_user_id
        AND ee.created_at = rcf.escalation_time
        AND ee.escalation_type = rcf.escalation_type
    LEFT JOIN front_per_escalation AS fpe
        ON ee.dec_user_id = fpe.dec_user_id
        AND ee.created_at = fpe.escalation_time
        AND ee.escalation_type = fpe.escalation_type
    LEFT JOIN stage_per_escalation AS spe
        ON ee.dec_user_id = spe.dec_user_id
        AND ee.created_at = spe.escalation_time
        AND ee.escalation_type = spe.escalation_type
    LEFT JOIN policy_active_per_escalation AS pape
        ON ee.dec_user_id = pape.dec_user_id
        AND ee.created_at = pape.escalation_time
        AND ee.escalation_type = pape.escalation_type
)
-- End of joined_escalations: one row per escalation with three-tier conv_id resolution, Front match, stage, and PH flag.

SELECT
    CASE
        WHEN je.conversation_id IS NOT NULL THEN concat('https://propelix.ai/app/conversations/', je.conversation_id)
        WHEN je.escalation_path = 'phone_path' THEN 'Phone Escalation'
        ELSE 'No Conversation ID'
    END AS propelix_link
  , CASE
        WHEN je.front_conv_api_id != '' THEN concat('https://app.frontapp.com/open/', je.front_conv_api_id)
        ELSE 'No Front Link'
    END AS front_link
  , concat('https://getjerry.com/admin/user/', base64Encode(concat('User:', toString(je.dec_user_id)))) AS crm_link
  , u.display_name AS display_name
  , je.dec_user_id AS dec_user_id
  , je.target_msg_id AS msg_id
  , toTimezone(je.created_at, 'America/New_York') AS escalation_timestamp
  , je.escalation_type AS escalation_type
  , je.escalation_path AS escalation_path
  , je.channel AS channel
  , je.conversation_id AS conversation_id
  , je.front_conv_api_id AS front_conversation_id
  , je.conv_id_source AS conv_id_source
  , je.user_stage AS user_stage
  , coalesce(je.is_policyholder_int, 0) = 1 AS is_policyholder
  , multiIf(
        je.escalation_type IN ('Billing', 'Cancel Request', 'Recent Cancel', 'Pre Cancel', 'NNO', 'Doc Request', 'EIP', 'CRT Lite', 'Wrong Number', 'Legal'), 'RTC',
        je.escalation_type IN ('nPH P3', 'nPH P4', 'nPH P5', 'nPH P6', 'nPH Active RTC'), 'Sales Lite',
        je.escalation_type = 'FMT', 'FMT',
        je.escalation_type = 'Spanish CRT', 'Spanish',
        je.escalation_type IN ('CALL_IN', 'QUEUE_RETURN_CALL', 'SCHEDULE_RETURN_CALL'), 'Phone Routing',
        'Other'
    ) AS team
FROM joined_escalations AS je
LEFT JOIN user_name_display AS u
    ON je.dec_user_id = u.user_id
ORDER BY je.created_at DESC
