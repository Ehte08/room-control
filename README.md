# room-control

It started, as most of my late-night ideas do, with a complaint.

I had to get up to turn off my lights every time I wanted to go to sleep. That was it. That was the whole problem. I'd be lying in bed, finally comfortable, and then — lights still on. So I figured: I have a computer, I have Python, how hard could this be?

I picked up a set of Govee smart bulbs and a smart AC plug. The bulbs worked great through the Govee app. But pulling out my phone to tap a button didn't feel much smarter than flipping a switch. I wanted to say "goodnight" to my room and have it actually listen.

---

## The Bluetooth Dead End

My first instinct was Bluetooth — the lights are right there, why route anything through the internet? I installed `bleak`, the Python async Bluetooth library, and spent an afternoon trying to pair, connect, and send commands directly to the bulbs. They paired. Then they dropped. Then they paired again and ignored every command I sent. Govee uses a proprietary Bluetooth protocol they have absolutely no interest in documenting, and I learned that the hard way.

I scrapped the whole approach and went through the Govee HTTP API instead. JSON payloads, clean request structure, retry logic for rate limits. It worked immediately. Within twenty minutes I had a Python script that could turn my lights red from the terminal.

But I still wanted voice control.

---

## Enter Jarvis

I pulled in Vosk — an offline speech recognition model — because I wasn't about to route my bedroom audio through someone's cloud server every time I spoke. The wake word was obvious. I named it Jarvis. Classic.

The first version just listened for commands directly. You'd say "turn the lights red" and it would fire. Except the word "red" would sometimes come through as "read," "head," or just not register at all. And I had this specific thing I wanted: say "goodnight," and the lights dim to red at 30%, the AC turns on, done. But "goodnight" kept matching nothing — the exact string comparison I'd written was too rigid. Say "goodnight room" instead of "goodnight" and it'd fail silently.

I needed something smarter.

---

## The Similarity Problem

That's when I brought in `rapidfuzz`. Instead of matching exact strings, I now match on similarity — anything scoring above 75 out of 100 gets executed. Close enough is good enough.

And if something fuzzy-matches successfully, the system *learns* it: that mapping gets saved to `aliases.json` so next time it's an exact hit, no fuzzy work needed. The file started empty. After a week of use, it had three entries — including one glorious one:

```
"the on the [unk] ac on the ac on [unk] [unk]" → "turn the ac on"
```

Vosk had a moment. The alias remembered it forever.

---

## I Just Wanted to Clap

The clap detection came after a night where I didn't feel like saying anything at all. The idea: double-clap to toggle between red mode and lounge mode, no words required.

I tapped into the same audio stream Vosk was already reading, chunked it into 50ms windows, and looked for any chunk where the peak amplitude crossed 15,000. If two spikes like that landed within two seconds of each other, I'd fire the toggle.

The tricky part was distinguishing a real clap from a loud word, a door slam, or music. A genuine clap is impulsive — its energy spikes hard in one 50ms chunk and then drops. Sustained sounds spread across multiple chunks. That distinction in the detection was what made it reliable.

---

## The Command Window Bug

The last real headache was timing. When Vosk heard "Jarvis," it opened a five-second window for the next command. But if I spoke too fast, the wake word and the command both showed up in the same recognition pass, and the system would miss the command entirely — it was still waiting for Jarvis to "finish" before listening for what followed.

The fix was to scan for early matches in the partial result stream. If "jarvis" appears in a partial and a command phrase follows it in the same partial, execute immediately without waiting for the final result to come back. That cut the lag noticeably and made fast speech actually work.

---

## What it does right now

Say `Jarvis` and then any of these:

| Command | What happens |
|---|---|
| `goodnight` / `sleep mode` | All lights to red at 30% brightness, AC turns on |
| `lounge mode` | All bulbs to 2700K warm white at 30% |
| `red mode` | All lights to full red |
| `good morning` | Warm white at 30%, AC turns off |
| `turn the ac on` / `turn the ac off` | AC plug only |

Or double-clap to toggle between red and lounge without saying a word.

Fuzzy matching means you don't have to be precise. Alias learning means the system gets better the longer you use it. Parallel API calls via `ThreadPoolExecutor` mean changing five bulbs takes the same time as changing one.

---

## Setup

```bash
git clone https://github.com/Ehte08/room-control.git
cd room-control
pip install -r requirements.txt

# Create a .env file with your Govee API key
echo "GOVEE_API_KEY=your_key_here" > .env

# Discover your device IDs and paste them into main.py
python main.py --discover

# Run it
python main.py
```

---

## Next steps

The dream is a Raspberry Pi tucked behind my desk — no laptop needed, just a mic, the device IDs, and this script running on boot. The code is already mostly there. The only things between me and that are finding a USB microphone that doesn't add noise at distance, and figuring out how to get Vosk's model to load fast enough on Pi hardware. Right now I'm waiting through a ~10-second startup delay that would feel a lot more acceptable if the Pi were always-on in the background.

When that lands, the lights won't even know I'm running it.
