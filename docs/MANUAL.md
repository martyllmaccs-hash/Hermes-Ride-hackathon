# Hermes-Ride: rider's manual

The short version for the person on the bike. Install and settings: `README.md`. Voice and model wiring:
`docs/VOICE-AND-MODELS.md`.

**Three gestures:** double tap = wake · swipe up, tap MUTE = mic mute · swipe up, hold the red bar 3 s = new session.

## Before the first ride (once)
1. Whoever runs your Hermes installs the plugin, sets `RIDE_ACCESS_KEY` and `ELEVENLABS_API_KEY`, and serves the page
   over HTTPS (for example `tailscale serve --bg --https=8447 http://127.0.0.1:8790`). See *Install* in the README.
2. On the iPhone, open the page in **Safari** (`https://<machine>.<tailnet>.ts.net:8447/`). Type the access key into
   the form once. It is remembered for 30 days.
3. Share → **Add to Home Screen**, keep *Open as Web App* on. Start Ride from that icon from now on: the home-screen
   app keeps the mic and the speaker running with the screen locked.
4. Pair the helmet headset with the phone.

## Starting a ride
1. Mount the phone with the screen facing you. Connect the headset.
2. Open Ride and **tap the pulsing orb once**. iOS asks for the microphone and for motion; allow both. (Without
   motion, lean and braking are off. Everything else works.)
3. The screen goes black. Ride is listening for the wake word only, and nothing leaves the phone.
4. Ride. The mount orientation calibrates itself: it finds "down" within about a minute and "forward" after the
   first few accelerations and stops.

## Talking
- Say **"Hey Hermes"** (or the wake word your setup uses). You hear the wake answer, and the orb turns blue.
- Ask anything you would ask your Hermes in chat: questions, web search, tasks, your notes. It is your own agent,
  with its memory and tools. It knows your speed, heading, position, lean angle and braking, and what happened since
  you last asked.
- After an answer the mic stays open for **10 seconds**, so follow-ups need no wake word.
- While it works you hear a soft **"doo, doo"** every few seconds.
- To interrupt a reply, just talk: three or more words cut in. "Yeah, okay" does not.
- **Double tap** anywhere on the screen to wake it without speaking. A double tap only wakes it: it never mutes,
  and while it is listening, thinking or speaking it does nothing.
- It never approves an action by voice. If your agent needs an approval, give it from Hermes Desktop or the chat
  app.

## What the orb tells you
| Orb | Meaning |
|---|---|
| Black | Asleep: waiting for the wake word |
| Blue, breathing | Listening |
| Bright white, then slow breaths | Thinking |
| Blue with a pulse | Speaking |
| Slow amber pulse | Connecting or offline (you also hear "Lost the connection, reconnecting.") |

The orb leans like a spirit level, squashes under braking and wobbles over bumps. That is the bike, not a fault.

## Mute
1. Swipe up from the grey grabber at the bottom edge. The red **RESET** bar and the **MUTE** button appear.
2. Tap **MUTE**. The button turns cyan and reads **UNMUTE**. Nothing is heard or sent, and neither the wake word nor a
   double tap wakes it.
3. Tap **UNMUTE** to listen again. There is no reload.

The headset's or lock screen's **pause** button mutes too, and **play** unmutes.
The panel hides by itself after 10 seconds untouched.

## Reset (a fresh conversation)
1. Swipe up from the grabber.
2. **Hold the red RESET – NEW SESSION bar for 3 seconds.** It fills while you hold. Letting go or sliding off
   cancels.
3. A short rising tone plays, the current answer stops, a new conversation starts, and the page reloads.
4. **Tap the orb once again** (iOS asks for this after every reload).

The old conversation stays in your Hermes history, ending with **Ride Summary?** Answer **Yes** in Hermes Desktop
(or type "yes") to get:
- the ride's character against your own past rides,
- a short story of the ride,
- ideas, fixes and things to keep, quoted from what you said.

If the reset fails you hear two low notes. The conversation keeps working, and you can hold the bar again.

## If something is off
| You hear or see | Do this |
|---|---|
| "I can't hear the mic." | Reload Ride and tap the orb once. Check that Safari still has mic permission. |
| "I'm offline. I'll keep trying." | No signal or no server. It reconnects by itself when the link is back. |
| "Speech recognition's out of quota. Switching to backup." | The speech-to-text plan is used up. Top it up, or ask your admin about the offline engine. |
| "tap 1 again" on screen | Audio or mic did not start. Tap the orb again. |
| It wakes when nobody said the wake word | Tell your admin. It is a bug, not a setting. |
