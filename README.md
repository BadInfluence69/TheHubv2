# TheHubv2

# The Hub v2

**A self-hosted, ad-free video and community platform. No algorithm, no gatekeeping, no tracking.**

<!-- TODO: replace yourusername/the-hub throughout with your actual repo path -->

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Self-Hosted](https://img.shields.io/badge/self--hosted-yes-success)
![Ads](https://img.shields.io/badge/ads-zero-critical)
![License](https://img.shields.io/badge/license-MIT-green)

---

## What It Is

The Hub is a self-hosted platform you run on your own machine or server. It gives you a
full video-watching experience backed by YouTube's catalog, plus the community and creator
features that mainstream platforms either buried, broke, or monetized into uselessness.

v2 is a ground-up rewrite of the original Hub. New architecture, new feature set — it is
not a patch on v1 and does not share its data layout.

## Why It Exists

Every design decision here answers a specific frustration:

| The problem | What The Hub does |
| --- | --- |
| Ads before, during, and after everything | Zero ads. Anywhere. There is no ad code in this project. |
| Sponsor segments inside the video itself | SponsorBlock is on by default, no setup required. |
| "Subscribe" no longer means you see the content | **Become a Fan** actually means you follow that channel. |
| Hidden dislike counts | Likes *and* dislikes are fully public on every video. |
| Monetization locked behind subscriber and watch-hour thresholds | Creators can monetize on day one. Fill in the fields at signup, done. |
| Platform takes a cut and controls the payout | Fans pay creators directly. |
| Your likes and comments broadcast to the wider internet | All activity stays inside your instance. Nothing is shared out. |
| DMs readable by the platform | Private messages are end-to-end encrypted by default. |

## Features

### Watching
- Video playback sourced from **YouTube**. The Hub is a client and community layer — it
  does not host video files of its own.
- **SponsorBlock enabled by default.** Sponsor segments, intros, and self-promos are
  skipped without you configuring anything.
- **No advertisements** of any kind, at any point, anywhere in the interface.
- Public like and dislike counts on every video.

### Community
- **Public chat rooms** open to everyone on your instance.
- **Sub-rooms** that members can create themselves for narrower topics, communities, or
  side conversations.
- **Direct messages** between individual users.
- **End-to-end encryption on every private message, by default.** Not a toggle, not a
  premium tier, not something you have to remember to switch on.

### Creators
- **Monetized from day one.** A new account fills in the payment fields during signup and
  can accept support immediately.
- **No thresholds.** No minimum fan count, no minimum watch hours, no review queue, no
  application, no waiting period.
- **Direct fan-to-creator payments.** <!-- TODO: name the payment method(s) you support — Stripe, PayPal, crypto, direct links, etc. -->
- **Become a Fan** subscribes you to a channel and means exactly that: you see what they
  post.

### Privacy
- Your comments, likes, and fan subscriptions are visible **only to other people on your
  instance.** Nothing is exported, syndicated, or pushed to any outside social network.
- No analytics, no telemetry, no third-party trackers.
- E2E-encrypted DMs by default.

## Requirements

- **Python 3.10 or higher** — required, earlier versions will not run.
- **ffmpeg** — install from [ffmpeg.org](https://ffmpeg.org/download.html) or your system
  package manager, and make sure it is on your `PATH`.
- <!-- TODO: list anything else — DB, Node, Redis, etc. -->

## Installation

> **Important:** the project is distributed as **two separate zip archives** because of
> GitHub's file and repository size limits. You need both. Extract the contents of *both*
> archives into the **same root folder** — the one you want the project to live in. The
> files are meant to sit alongside each other in that root, not in separate subfolders.

1. **Download both archives** from the repository (or clone it and unpack them in place).

2. **Extract both into your chosen project root.** When you're done, everything from
   archive one and archive two should be in that single folder together.

3. **Verify ffmpeg is available:**

```bash
   ffmpeg -version
```

4. **Install the Python dependencies:**

```bash
   cd /path/to/the-hub
   pip install -r requirements.txt
```

5. **Run it:**

```bash
   python app.py
```
   <!-- TODO: correct the entry command if it's different -->

6. **Open it in your browser:**

```
   http://localhost:5000
```
   <!-- TODO: correct the port if it's different -->

## Configuration

<!-- TODO: document your config file / env vars — e.g. secret key, DB path, media paths,
     port, admin account creation, payment provider keys -->

## Creator Setup

1. Create an account on your instance.
2. Fill in the payment fields on the account form.
3. That's it — you're monetized. There is nothing to apply for and nobody to approve you.

## Notes

- The Hub does not host, store, or redistribute video content. Video is played from
  YouTube. This project is a self-hosted client and community layer on top of it.
- Because it's self-hosted, your instance is yours. Your users, your rules, your data,
  your server.

## License

Released under the MIT License — use it, fork it, ship it, do what you want with it.
<!-- TODO: add a LICENSE file to the repo if you haven't yet -->

## Keywords

`self-hosted` `youtube-client` `ad-free` `sponsorblock` `end-to-end-encryption`
`creator-monetization` `chat-rooms` `privacy` `video-platform` `no-ads` `python`
