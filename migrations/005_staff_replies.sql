-- Two more kinds of message to the customer:
--   handoff      the automatic note sent when a conversation is handed to a person;
--   staff_reply  a reply a staff member wrote in the console.

ALTER TABLE outbound_messages DROP CONSTRAINT outbound_messages_kind_check;
ALTER TABLE outbound_messages ADD CONSTRAINT outbound_messages_kind_check CHECK (kind IN (
    'ack', 'follow_up', 'summary', 'price_quote', 'cancellation_ack', 'redirect', 'handoff', 'staff_reply'));
