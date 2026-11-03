-- Fair claiming across shops (jobqueue.claim): a free worker serves the shop with the fewest jobs
-- running, and on a tie the shop it served longest ago. "Fewest running" alone isn't enough with
-- one worker: nothing is running when it picks, every shop ties, and a flood from one shop would
-- still be served first. claimed_at records each claim so the tie can go to the shop served
-- longest ago; the index makes "when did we last serve this shop" a single index lookup.
--
-- A new migration rather than an edit to 001: applied migrations are immutable (see db.py).
ALTER TABLE jobs ADD COLUMN claimed_at timestamptz;
CREATE INDEX jobs_shop_claimed_idx ON jobs (shop_id, claimed_at);
