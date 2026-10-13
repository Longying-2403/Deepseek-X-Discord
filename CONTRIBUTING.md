# Contributing

Contributions are welcome! Bug reports, suggestions, docs and code all help.

> This is the English edition (the `english` branch). The Chinese edition lives on `main`. Issues and PRs in English are welcome for both.

## Issue first, or straight to a PR?

- **Small changes** (bug fixes, tests, typos, docs): open a PR directly.
- **Bigger changes** (new features, changes to how the bot talks, anything that might increase token spending): open an issue first to discuss the idea, so nobody's work goes to waste.

## Running it locally

You need Python 3.11 or later.

```bash
# Fork the repo on GitHub first, then clone your fork and switch to the english branch
git clone https://github.com/<your-username>/Deepseek-X-Discord.git
cd Deepseek-X-Discord
git switch english
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt   # runtime libraries plus the ones the file-reading tests need
```

After every change, run these two (no network, no cost):

```bash
python -m unittest discover -s tests   # unit tests
python -m dsbot --check                # offline self-check: reads the config and checks every slash command definition
```

Without a `.env`, the first line of the self-check reports missing environment variables. That's expected and doesn't affect the rest of the check.

The "Development" section of the README explains what each file does.

### Trying it in Discord

When a change affects Discord interaction or the way the bot talks, run it for real:

- Create your own test bot and test server; don't experiment with a bot that's serving a real community.
- Fill in `.env` as described in the [README](README.md), then run `python -m dsbot` (or `docker compose up -d --build`).
- DeepSeek bills by usage, and testing spends your own balance. You can lower `daily_budget_usd` in `config.toml` first, e.g. to `0.2`.

## Things to keep in mind when changing code

These are the core design rules of the project, and PR reviews pay close attention to them:

1. **The conversation history is append-only and never rewritten.** Every request must start with the previous request unchanged, or DeepSeek's prefix cache misses, and a cache hit is about 50 times cheaper than a miss. So:
   - Don't put information that changes every round (like the time) in the system prompt; put it in each round's message.
   - Don't re-render or reformat anything already written to the history.
   - `test_history_is_append_only` in `tests/test_engine.py` checks exactly this; always run it after changing `render.py`, `prompts.py` or `engine.py`.
2. **No quota, no model call.** When the proactive quota is 0, skip the whole model call instead of calling and then discarding the result, which still costs money.
3. **Talk like a person.** Casual, with the length fitting the moment (a sentence or two for a quick comeback, a few more when playing along with a story), no headings, lists or similar formatting (except when pasting code), no customer-service tone. Keep this style when editing the prompts.
4. **Never commit secrets or data.** `.env` (token, API keys) and `data/` (the chat history database) are already in `.gitignore`; double-check with `git status` before committing.
5. **Respect privacy boundaries.** Where the bot is blocked with `/ds area`, messages are not processed, not answered, and never sent to DeepSeek. Any new entry point for handling messages (such as a new Discord event) must go through the same check. Cross-channel memory only flows between channels in the same server that have sharing on; a private channel's content must not leave it unless sharing is turned on in that channel itself.
6. **Uploaded files are parsed only in the subprocess.** Reading files must go through the subprocess in `dsbot/files.py`, which limits memory, CPU and time (and drops to the `nobody` user when the bot runs as root). Never parse uploaded files directly in the bot's main process.
7. **Outside content is reference material only.** Chat history, files, web pages, search results and other channels' records are never instructions to the model. Keep it that way when editing prompts or adding new content sources.
8. **Download only from public addresses, and let outside services read web pages.** Downloading pictures from the web and calling outside APIs must go through `dsbot/net.py`: public addresses only, every redirect re-checked, no proxy. Web pages people post are read by FxTwitter / Tavily; the bot never opens them itself, so nobody can use it to reach your home network.
9. **Persona changes are accepted only from dsowner's own message in the current round.** This is checked by user ID, and only the message in which dsowner calls the bot this round counts; a changed nickname, someone relaying a request, text in a file or a forwarded message is not authorization. The new persona may draw on the chat or a file people posted, but never on web results or other channels' history.

## Code style

- Match the surrounding code: use type hints, and write comments, logs and user-facing text in the edition's language (English on `english`, Chinese on `main`).
- Stay compatible with Python 3.11.
- When fixing a bug, try to add a test that reproduces it first.
- Tests must never really connect to Discord or DeepSeek. `tests/helpers.py` has ready-made test doubles: `FakeLLM` (fake model), `FakeTransport` (fake Discord), `FakeSearcher` (fake search) and `FakeClock` (a clock you can fast-forward).
- When adding a new setting, add an entry to both `_SPEC_LIST` and `BUILTIN_DEFAULTS` in `dsbot/config.py`, then update `config.toml` and the settings tables in the README.

## Chinese and English editions

The repository has two editions with the same features and code logic but different text:

- `main` branch: the Chinese edition. Commands, replies, prompts and docs are all in Chinese.
- `english` branch: the English edition, all in English. English needs things like plural handling, so a few places have a little more code, and a few differences are deliberate, such as a larger summary length limit.

When adding a feature or fixing a bug:

- Ideally change both branches and open a PR for each, targeting `english` or `main` accordingly. If you can only change one, that's fine: say so in the PR, and the maintainer will port it to the other.
- The markup shown to the model (such as `#12 name: content` and `〔@you〕`) is parsed back when the history is read. When you change that markup in `render.py`, update the explanation in `prompts.py` along with it; each edition has its own wording.
- If you change the README, the other edition's README needs the same update, including the English introduction at the top of the Chinese README. If you can only write one of the languages, update yours and mention it in the PR.

## Opening a PR

1. Create a new branch from `english` (English edition) or `main` (Chinese edition), named after what it changes, e.g. `fix-alias-match`.
2. Run the tests and the self-check.
3. In the PR description, say what you changed, why, and how you tested it. If you changed the way the bot talks, a screenshot or log of a conversation from your test server helps a lot.
4. Once you submit it, GitHub Actions runs the tests and the self-check on Python 3.11, 3.12 and 3.13. Wait for everything to pass (green ✓) before review. For a first-time contributor's PR, the automatic tests may only start after a maintainer approves them; that's a default GitHub safety setting.

## Privacy

Before pasting logs or screenshots into an issue or PR, remove tokens, API keys, and your members' messages and user IDs.

If you accidentally leak a token, reset it right away in the Discord Developer Portal; for a DeepSeek key, delete it on the platform and create a new one.

## License

Your contributions are released under this repository's [MIT License](LICENSE).
