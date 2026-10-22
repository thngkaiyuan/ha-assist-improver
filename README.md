<p align="center"><img src="assets/icon.png" width="128" alt=""></p>

# Assist Improver

**Your Home Assistant's AI, easier every week.**

> 🚧 In development, and currently being tested in my household on Home Assistant 2026.10.2.

<p align="center"><img src="assets/smoother.png" alt="Saying you are cold: three turns today, one turn with exact words after the first improvement, one natural turn after the second"></p>

## How it works

Think of a self-driving car. Every time the driver takes the wheel back, that's an *intervention*, and each one teaches the car where it still falls short.

<p align="center"><img src="assets/intervention.gif" width="480" alt="A relaxed driver suddenly panics and grabs the wheel"></p>

The best self-driving is one you never feel the need to supervise: **you just enjoy the ride.**

Assist Improver aims for that with your assistant. The goal is to cut the effort it takes you to get what you wanted, whether that's a change in your home or an answer to a question. Today it measures effort as the extra turns you spend repeating, correcting or clarifying, and that measure will improve over time.

1. **Learns from your own use.** Keeps your Assist conversations and their results in your Home Assistant, next to the history it already keeps. It's as private as your Home Assistant setup.
2. **Thinks.** A deep-thinking AI looks for where you had to step in and proposes changes that would have spared you the effort.
3. **Tests.** Replays the same conversations with each change, as a counterfactual experiment. Devices are simulated, so nothing in your home moves.
4. **Asks you.** Only changes that proved they save effort reach you. You approve, ask for a revision, or decline.

Approved improvements stay even if you uninstall Assist Improver.

## Where we are

- [x] Record conversations
- [x] Replay a conversation safely, with simulated devices
- [ ] Prepare conversations for AI analysis, and choose the AI model **<===== we are here**
- [ ] Find journeys and interventions
- [ ] Spot recurring problems and propose fixes
- [ ] Build and test the fixes
- [ ] Review and apply them in Home Assistant
- [ ] Run on its own schedule
- [ ] Ready for other households

## Install

Through [HACS](https://hacs.xyz):

1. HACS → ⋮ → **Custom repositories**: add this repository's URL, type **Integration**.
2. Download **Assist Improver**, then restart Home Assistant.
3. **Settings → Devices & services → Add integration → Assist Improver**, and pick the AI model it should use. We recommend keeping the recommended option for better results.

## Your data and costs

- Your conversations stay in your Home Assistant, as private as the rest of your setup.
- Analysis sends a summary of them to the AI model you chose, through Home Assistant's own AI Task. It runs only when you allow it, because it can cost money with your AI provider.

## License

[Apache 2.0](LICENSE)
