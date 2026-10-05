-- How many times a unit was sent to the phone. A poll reply can be lost after
-- the server assigned a unit; the phone then polls idle again and gets the
-- same unit back (up to a limit) instead of the loss counting as a crash.
ALTER TABLE units ADD COLUMN IF NOT EXISTS deliveries INTEGER NOT NULL DEFAULT 0;
