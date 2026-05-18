-- Self-serve / escalation rate query for the escalation_clusters master sheet.
-- Forked from Card 14466 (Chatbot Total Messages V4) -- the canonical chatbot self-serve definition.
-- Returns one row for the date range: total_bot_convs, escalated_convs, self_serve_convs, escalation_rate, self_serve_rate.
-- count(DISTINCT) preserved from source for fidelity; do not refactor without re-validating against 14466.
-- Numbers verified to match Card 14466 to the row for week of 2026-04-24 (10,796 bot / 8,049 self-serve).
-- Anti-join pattern (oou.dec_user_id IS NULL / exu.dec_user_id IS NULL) is safe: events.track_all.dec_user_id is Nullable(Int64)
-- so LEFT JOIN no-matches return NULL, not the corrections-log [2026-03-25] non-Nullable type-default trap.

WITH
-- Start of text_messages_filtered
-- Purpose: User text messages within the date range, with junk text dropped.
text_messages_filtered AS (
    SELECT
        msg_id
      , dec_user_id
      , msg_timestamp
      , text
      , channel
      , author
    FROM magic.text_messages
    PREWHERE msg_timestamp >= toDateTime({{start_date}}) - INTERVAL 1 DAY
    WHERE 1
        AND lower(text) NOT IN ('push', '"push"', 'call me')
        AND toDate(toTimezone(msg_timestamp, 'America/New_York')) BETWEEN {{start_date}} AND {{end_date}}
)
-- End of text_messages_filtered: text_messages within the date range minus junk strings.

-- Start of msg
-- Purpose: Filtered messages joined to user_stage_history; restricts to stages 0,1,3,4,9,10,11,12 per Card 14466.
, msg AS (
    SELECT
        a.msg_id AS msg_id
      , a.dec_user_id AS uid
      , a.msg_timestamp AS timestamp
      , a.text AS text
      , a.channel AS channel
      , a.author AS author
      , b.stage AS stage
    FROM text_messages_filtered AS a
    LEFT JOIN magic.mv_user_stage_history AS b
        ON toString(a.dec_user_id) = toString(b.user_id)
    WHERE 1
        AND a.msg_timestamp >= b.start_timestamp
        AND (a.msg_timestamp <= b.end_timestamp OR b.end_timestamp IS NULL)
        AND b.stage IN (0, 1, 3, 4, 9, 10, 11, 12)
)
-- End of msg: filtered messages with stage attached.

-- Start of direct_to_call_messages
-- Purpose: Call Agent Action CALL_IN events keyed for user-day-channel join.
, direct_to_call_messages AS (
    SELECT
        dec_user_id
      , created_at
      , event_label AS action
      , JSONExtractString(event_metadata, 'targetMessageID') AS target_msg_id
      , JSONExtractString(event_metadata, 'channel') AS channel
      , JSONExtractString(event_metadata, 'hasAttachments') AS call_in_attachments
    FROM events.track_all
    PREWHERE created_at >= toDateTime({{start_date}}) - INTERVAL 1 DAY
    WHERE 1
        AND event_category = 'Chatbot'
        AND event_action = 'Call Agent Action'
        AND event_label = 'CALL_IN'
        AND toDate(toTimezone(created_at, 'America/New_York')) BETWEEN {{start_date}} AND {{end_date}}
)
-- End of direct_to_call_messages: CALL_IN events.

-- Start of add_to_queue_messages
-- Purpose: Call Agent Action QUEUE_RETURN_CALL events keyed for user-day-channel join.
, add_to_queue_messages AS (
    SELECT
        dec_user_id
      , created_at
      , event_label AS action
      , JSONExtractString(event_metadata, 'channel') AS channel
      , JSONExtractString(event_metadata, 'hasAttachments') AS queue_attachments
    FROM events.track_all
    PREWHERE created_at >= toDateTime({{start_date}}) - INTERVAL 1 DAY
    WHERE 1
        AND event_category = 'Chatbot'
        AND event_action = 'Call Agent Action'
        AND event_label = 'QUEUE_RETURN_CALL'
        AND toDate(toTimezone(created_at, 'America/New_York')) BETWEEN {{start_date}} AND {{end_date}}
)
-- End of add_to_queue_messages: QUEUE_RETURN_CALL events.

-- Start of schedule_call
-- Purpose: Call Agent Action SCHEDULE_RETURN_CALL events keyed for user-day-channel join.
, schedule_call AS (
    SELECT
        dec_user_id
      , created_at
      , event_label AS action
      , JSONExtractString(event_metadata, 'channel') AS channel
    FROM events.track_all
    PREWHERE created_at >= toDateTime({{start_date}}) - INTERVAL 1 DAY
    WHERE 1
        AND event_category = 'Chatbot'
        AND event_action = 'Call Agent Action'
        AND event_label = 'SCHEDULE_RETURN_CALL'
        AND toDate(toTimezone(created_at, 'America/New_York')) BETWEEN {{start_date}} AND {{end_date}}
)
-- End of schedule_call: SCHEDULE_RETURN_CALL events.

-- Start of all_call
-- Purpose: Combined call-action events keyed by targetMessageID for direct-to-call attachment detection.
, all_call AS (
    SELECT
        dec_user_id
      , created_at
      , event_label AS action
      , JSONExtractString(event_metadata, 'targetMessageID') AS target_msg_id
      , JSONExtractString(event_metadata, 'channel') AS channel
      , JSONExtractString(event_metadata, 'hasAttachments') AS call_in_attachments
    FROM events.track_all
    PREWHERE created_at >= toDateTime({{start_date}}) - INTERVAL 1 DAY
    WHERE 1
        AND event_category = 'Chatbot'
        AND event_action = 'Call Agent Action'
        AND event_label IN ('CALL_IN', 'QUEUE_RETURN_CALL', 'SCHEDULE_RETURN_CALL')
        AND toDate(toTimezone(created_at, 'America/New_York')) BETWEEN {{start_date}} AND {{end_date}}
)
-- End of all_call: combined call-action events for msg-level join.

-- Start of bot_reply
-- Purpose: Reply Sent + Handed off events keyed to the user message they target.
, bot_reply AS (
    SELECT
        JSONExtractString(event_metadata, 'targetMessageID') AS target_msg
      , event_action
      , dec_user_id
      , event_id
      , JSONExtractString(event_metadata, 'reason') AS handed_off_reason
      , JSONExtractString(event_metadata, 'escalationType') AS escalation_type
      , JSONExtractString(event_metadata, 'hasAttachments') AS handed_off_attachment
    FROM events.track_all
    PREWHERE created_at >= toDateTime({{start_date}}) - INTERVAL 1 DAY
    WHERE 1
        AND event_category = 'Chatbot'
        AND event_action IN ('Handed off', 'Reply Sent')
        AND toDate(toTimezone(created_at, 'America/New_York')) BETWEEN {{start_date}} AND {{end_date}}
)
-- End of bot_reply: bot-side Reply Sent and Handed off events.

-- Start of excluded_escalation_users
-- Purpose: User-days where user had a Postpone Payment or Pre Cancel Direct escalation; excluded from SS denominator.
, excluded_escalation_users AS (
    SELECT
        dec_user_id
      , toStartOfDay(toTimezone(created_at, 'America/New_York')) AS exclusion_date
    FROM events.track_all
    PREWHERE created_at >= toDateTime({{start_date}}) - INTERVAL 1 DAY
    WHERE 1
        AND event_category = 'Chatbot'
        AND event_action IN ('Handed off', 'Reply Sent')
        AND JSONExtractString(event_metadata, 'escalationType') IN ('Postpone Payment', 'Pre Cancel Direct')
        AND toDate(toTimezone(created_at, 'America/New_York')) BETWEEN {{start_date}} AND {{end_date}}
    GROUP BY dec_user_id, exclusion_date
)
-- End of excluded_escalation_users: user-day rows for SS-excluded escalation types.

-- Start of opt_out_users
-- Purpose: User-days where user opted out of marketing comms; excluded from SS denominator.
, opt_out_users AS (
    SELECT
        dec_user_id
      , toStartOfDay(toTimezone(created_at, 'America/New_York')) AS opt_out_date
    FROM events.track_all
    PREWHERE created_at >= toDateTime({{start_date}}) - INTERVAL 1 DAY
    WHERE 1
        AND event_category = 'Chatbot'
        AND event_action = 'Call Agent Action'
        AND event_label = 'SMS_MARKETING_OPT_OUT'
        AND toDate(toTimezone(created_at, 'America/New_York')) BETWEEN {{start_date}} AND {{end_date}}
    GROUP BY dec_user_id, opt_out_date
)
-- End of opt_out_users: user-day rows for marketing opt-outs.

-- Start of daily
-- Purpose: Per-day, per-channel aggregates mirroring the metrics needed for SS rate computation.
, daily AS (
    SELECT
        toStartOfDay(toTimezone(timestamp, 'America/New_York')) AS et_day
      , msg.channel AS channel
      , count(DISTINCT if(event_action = 'Reply Sent' AND oou.dec_user_id IS NULL AND exu.dec_user_id IS NULL, bot_reply.dec_user_id, NULL)) AS num_bot_convs
      , count(DISTINCT CASE
            WHEN
                ((ac.action IS NOT NULL)
                OR (text LIKE '%+1 270-612-2227%')
                OR (text LIKE '%+1-833-445-3779%')
                OR (text LIKE '%+1-270-612-2227%')
                OR (text LIKE '%Please respond "CALLBACK" if you want to receive a call from an agent once one becomes available%')
                OR (text LIKE '%call button on the top right of your screen%')
                OR (text LIKE '%phone icon in the top right corner of your screen%'))
                AND exu.dec_user_id IS NULL
            THEN uid
        END) AS num_dtc_convs
      , count(DISTINCT if(event_action = 'Handed off'
            AND escalation_type != ''
            AND exu.dec_user_id IS NULL, bot_reply.dec_user_id, NULL)) AS num_handed_off_convs
      , count(DISTINCT CASE
            WHEN
                (
                    sc.action = 'SCHEDULE_RETURN_CALL'
                    OR dtc.action = 'CALL_IN'
                    OR aqm.action = 'QUEUE_RETURN_CALL'
                    OR text LIKE '%+1 270-612-2227%'
                    OR text LIKE '%+1-833-445-3779%'
                    OR text LIKE '%+1-270-612-2227%'
                    OR text LIKE '%Please respond "CALLBACK" if you want to receive a call from an agent once one becomes available%'
                    OR text LIKE '%call button on the top right of your screen%'
                    OR text LIKE '%phone icon in the top right corner of your screen%'
                )
                AND escalation_type != ''
                AND exu.dec_user_id IS NULL
            THEN uid
        END) AS num_handed_off_and_dtc_convs
    FROM msg
    LEFT JOIN bot_reply
        ON msg.msg_id = bot_reply.target_msg
    LEFT JOIN opt_out_users AS oou
        ON msg.uid = toString(oou.dec_user_id)
        AND toStartOfDay(toTimezone(msg.timestamp, 'America/New_York')) = oou.opt_out_date
    LEFT JOIN excluded_escalation_users AS exu
        ON msg.uid = toString(exu.dec_user_id)
        AND toStartOfDay(toTimezone(msg.timestamp, 'America/New_York')) = exu.exclusion_date
    LEFT JOIN direct_to_call_messages AS dtc
        ON toString(msg.uid) = toString(dtc.dec_user_id)
        AND toDate(msg.timestamp) = toDate(dtc.created_at)
        AND msg.channel = dtc.channel
    LEFT JOIN add_to_queue_messages AS aqm
        ON toString(msg.uid) = toString(aqm.dec_user_id)
        AND toDate(msg.timestamp) = toDate(aqm.created_at)
        AND msg.channel = aqm.channel
    LEFT JOIN schedule_call AS sc
        ON toString(msg.uid) = toString(sc.dec_user_id)
        AND toDate(msg.timestamp) = toDate(sc.created_at)
        AND msg.channel = sc.channel
    LEFT JOIN all_call AS ac
        ON ac.target_msg_id = msg.msg_id
    GROUP BY et_day, msg.channel
)
-- End of daily: user-day-channel grain with bot-conv, handoff, and direct-to-call counts.

-- Start of totals
-- Purpose: Sum daily metrics across the full date range.
, totals AS (
    SELECT
        sum(num_bot_convs) AS total_bot_convs
      , sum(num_handed_off_convs) AS handed_off_convs
      , sum(num_dtc_convs) AS directed_to_call_convs
      , sum(num_handed_off_and_dtc_convs) AS handed_off_and_dtc_convs
    FROM daily
)
-- End of totals: range-level sums of the four input metrics.

-- Start of derived
-- Purpose: Derive escalated_convs and self_serve_convs from totals (each computed once).
, derived AS (
    SELECT
        total_bot_convs
      , handed_off_convs
      , directed_to_call_convs
      , handed_off_and_dtc_convs
      , handed_off_convs + directed_to_call_convs - handed_off_and_dtc_convs AS escalated_convs
      , total_bot_convs - (handed_off_convs + directed_to_call_convs - handed_off_and_dtc_convs) AS self_serve_convs
    FROM totals
)
-- End of derived: totals with escalated_convs and self_serve_convs.

SELECT
    total_bot_convs
  , handed_off_convs
  , directed_to_call_convs
  , handed_off_and_dtc_convs
  , escalated_convs
  , self_serve_convs
  , if(total_bot_convs = 0, 0, self_serve_convs / total_bot_convs) AS self_serve_rate
  , if(total_bot_convs = 0, 0, escalated_convs / total_bot_convs) AS escalation_rate
FROM derived
