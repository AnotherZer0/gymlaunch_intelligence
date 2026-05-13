-- SMS infrastructure schema
-- Stores inbound Twilio messages and delivery status callbacks.
-- Depends on: 001_foundation.sql

BEGIN;

-- One row per inbound SMS received by any of our Twilio numbers.
-- channel is derived from the To number via the Lambda's TWILIO_NUMBER_CHANNELS mapping.
-- opt_out_keyword / opt_in_keyword are populated if the body matched a CTIA keyword.
-- hubspot_update_status is 'ok', 'contact_not_found', or an error string.
CREATE TABLE IF NOT EXISTS sms_inbound_message (
    id                    BIGSERIAL PRIMARY KEY,
    message_sid           TEXT        NOT NULL UNIQUE,  -- Twilio MessageSid
    from_number           TEXT        NOT NULL,          -- E.164 sender
    to_number             TEXT        NOT NULL,          -- E.164 our Twilio number
    body                  TEXT,
    channel               TEXT,                          -- Marketing | Product Updates | Unknown
    opt_out_keyword       TEXT,                          -- STOP / UNSUBSCRIBE etc, if matched
    opt_in_keyword        TEXT,                          -- START / UNSTOP / YES, if matched
    hubspot_contact_id    TEXT,                          -- VID of matched contact
    hubspot_update_status TEXT,                          -- 'ok' | 'contact_not_found' | error
    forwarded_to_octopods BOOLEAN     NOT NULL DEFAULT false,
    octopods_status_code  INT,
    raw_payload           JSONB,
    received_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sms_inbound_from_idx
    ON sms_inbound_message (from_number, received_at DESC);

CREATE INDEX IF NOT EXISTS sms_inbound_channel_idx
    ON sms_inbound_message (channel, received_at DESC);

CREATE INDEX IF NOT EXISTS sms_inbound_received_idx
    ON sms_inbound_message (received_at DESC);

-- Partial index for opt-out analysis — what caused a particular campaign's opt-outs
CREATE INDEX IF NOT EXISTS sms_inbound_opt_outs_idx
    ON sms_inbound_message (channel, received_at DESC)
    WHERE opt_out_keyword IS NOT NULL;

-- sms_delivery_event intentionally omitted in Phase 1.
-- Status callbacks are processed for HubSpot updates only (hard failures → geo block
-- suppression) but not stored. Full outbound message tracking — with body, sent_at,
-- and status — will be added in Phase 3 when we control outbound sends.

COMMIT;
