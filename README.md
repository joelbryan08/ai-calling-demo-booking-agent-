# Demo Booking Agent - Step 2: dialer (Plivo)

    pip install -r requirements.txt
    copy .env.example .env             # Windows  (Mac/Linux: cp .env.example .env)
    uvicorn app.main:app --reload      # docs at http://localhost:8000/docs
    pytest

## Try it safely (no real calls)
Keep `TELEPHONY_PROVIDER=fake` and set `DEFAULT_CALLER_NUMBER` in `.env` (any number).
1. POST /campaigns -> upload a CSV -> POST /campaigns/{id}/start
2. Within ~5 seconds the dialer "places" calls (up to max_parallel). See them at GET /campaigns/{id}/calls
3. Finish a call: POST /dev/calls/{call_id}/simulate?status=completed | busy | no-answer | failed
   - completed = answered (lead done); busy / no-answer / failed = retried after retry_gap_minutes, until max_attempts
4. The campaign flips to `completed` when no lead is pending or in progress.

## Real calls with Plivo
Set in `.env`: TELEPHONY_PROVIDER=plivo, PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN, a long random WEBHOOK_SECRET,
DEFAULT_CALLER_NUMBER (a number you own in Plivo), and PUBLIC_BASE_URL (an https tunnel such as ngrok that points at this app).
Test only on your own phone first. For now the answered call just speaks a short test message; step 3 connects the Sarvam voice agent.

## What the dialer does
- every DIALER_INTERVAL_SECONDS: for each running campaign, fills free slots (max_parallel) with pending leads
- skips do-not-call numbers; respects the calling window in each lead's timezone (waits until it opens)
- Plivo reports the result to /plivo/answer and /plivo/hangup (token-protected); duplicates are ignored
- calls that never report back are marked failed after STALE_CALL_MINUTES and retried

## Step 3a - agent tools (calendar scheduling + sheet logging)
Endpoints the voice agent will call mid-call. All need `Authorization: Bearer <TOOLS_API_KEY>`
(in /docs click **Authorize** and paste `dev-tools-key`).

- POST /tools/check_availability  {start_date, end_date?, timezone, time_of_day?} -> up to 3 free slots
- POST /tools/book_demo           {slot_start, timezone, prospect_name, prospect_email, company?, phone?} -> books it
- POST /tools/log_call            {phone, outcome, prospect_name?, company?, demo_time?, pain_points?, notes?, interaction_id?}

How a slot is assigned: every presenter's calendar is checked (demo + buffer must be free); among the free ones the
person with the FEWEST demos in the next 7 days gets it, ties go to whoever was assigned longest ago; anyone at
MAX_DEMOS_PER_DAY is skipped. The slot is re-checked at booking time. Unreadable calendar = treated as busy.

Try it (fake calendar, no Google): set PRESENTERS in .env to three emails, restart, then in /docs:
1. check_availability {"start_date": "<tomorrow>"}  -> copy one slot_start
2. book_demo with that slot_start + a name and email -> status booked
3. book it again with other emails: each new prospect goes to a different presenter; the 4th gets slot_taken
4. log_call -> a row appears in fake_sheet.csv in the project folder
Block a presenter: POST /tools/dev/fake_busy?presenter=a@x.com&start=2026-11-24T10:00:00%2B05:30&minutes=60

Going live with Google: `pip install -r requirements-google.txt`, set CALENDAR_BACKEND=google,
GOOGLE_SERVICE_ACCOUNT_FILE, SHEET_ID (share the sheet with the service account), and a long random TOOLS_API_KEY.
Sheet columns: time, phone, name, company, outcome, demo_time, pain_points, notes, interaction_id,
presenter, meeting_link  (the last two are filled only for booked calls).
If a Sheets write fails the log is kept in the database; POST /tools/retry_sheet_logs re-sends it.

## Step 3b - end-of-call hook (for the managed Sarvam voice agent)
- check_availability now also accepts `interaction_id`, returns a ready-to-speak `speech` sentence, and remembers
  which slots were offered on that call.
- POST /tools/call_end  {interaction_id, phone, call_outcome, prospect_name, company, prospect_email, chosen_slot,
  pain_points, notes} - run once when the call ends. If the caller agreed to a time it matches `chosen_slot` to the
  offered slots, books it, and logs the call. If matching or booking fails the call is logged as `callback` with
  "BOOKING NEEDS FOLLOW-UP" in notes, so nothing is silently lost. Safe to retry (idempotent).
- See sarvam_setup_guide.md for the agent prompt, variables and tool settings.

## Step 4 - live voice agent (your own pipeline)
Plivo streams the call audio to /voice/ws; each call runs Sarvam streaming STT -> Sarvam LLM (with tools) -> Sarvam TTS.
The agent books live: check_availability / book_demo / log_call / end_call run straight against the scheduling code.

1. `pip install -r requirements.txt`, then set SARVAM_API_KEY (and the other voice settings) in .env
2. `python scripts/check_sarvam.py`  - checks your key, TTS voice, LLM tool calling and the STT connection
3. Set VOICE_MODE=stream, keep TELEPHONY_PROVIDER=plivo and a working tunnel (PUBLIC_BASE_URL), restart
4. Run a campaign with only your own number. Your phone rings, the AI talks, the booking lands in the (fake) calendar and fake_sheet.csv
5. After the call: GET /campaigns/{id}/calls shows the transcript; fake_sheet.csv has the row with presenter and meeting link

Tuning knobs (.env): STT_SILENCE_MS (how long a pause ends the caller's turn), LLM_REASONING_EFFORT (blank = fastest),
TTS_SPEAKER, MAX_CALL_SECONDS. The prompt is in app/voice/prompt.py.

## Step 5 - the short Dexkor call script
Flow: "Hi, this is Diya from Dexkor. Are you having any problems handling customer churn and support?"
 -> yes: one-sentence pitch + "Can we book a quick 15-minute demo?" -> "What day and time suits you?" (never lists slots)
 -> caller names a time: book_demo checks all presenters and books whoever is free (least loaded first)
 -> caller has no preference: the agent proposes ONE time (from check_availability) and books it if accepted
 -> time taken: the agent proposes one nearby time
 -> no email is asked. "Is this number on WhatsApp? Our team will send the meeting details and follow up there."
 -> goodbye. The sheet row has outcome, demo time, presenter, meeting link and a "WhatsApp: ..." note.
A "yes" to the WhatsApp question sets whatsapp_consent on the lead. Edit the wording in app/voice/prompt.py and the
pitch in PRODUCT_PITCH (.env). Set DEMO_MINUTES=15, COMPANY_NAME=Dexkor, PROBLEM_AREA=customer churn and support.
NOTE: nothing sends the WhatsApp message automatically yet; until that module exists, your team follows up from the sheet.

## Step 6 - dashboard
Set ADMIN_PASSWORD in .env, restart, open http://localhost:8000/ and sign in. Campaign, do-not-call and /api endpoints now need that login (the UI handles it).

## Step 7 - dashboard upgrades
Live page (calls in progress, same process only), 14-day chart and demos-per-presenter on Overview, Settings page (script, opening line, extra rules; applies from the next call; stored in the database) and browser-side brand colours.

## Step 8
Calls: voicemail detection, silence nudge then goodbye, Hindi/Hinglish (AGENT_LANGUAGE=auto; voice follows the caller). Dashboard: frosted-glass redesign.

## Step 9
Call summaries (AI summary, sentiment, pain points, next step, shown in the call window) and recordings (RECORD_CALLS=true; stereo WAV in RECORDINGS_DIR, played from the call window; the opening line adds RECORDING_NOTICE).
