# Moving the gold bot to GitHub (about 10 minutes)

The bot will run on GitHub's servers every weekday at 06:45 AST, so the laptop can be off or unplugged. It is still **paper trading only**.

## Why the repository should be Public
- A run lasts about 4 to 5 hours (06:45 AST until the NY window closes).
- Private repositories get 2,000 free minutes a month. The bot needs about 5,500, so a private repository would stop around the 8th trading day of each month.
- Public repositories run for free with no limit.
- **What a Public repository shows:** the bot's code, and its paper journal and logs (dates, trades, paper equity). **What it does not show:** your Alpaca keys. Those go into GitHub "Secrets", which nobody can read, you included, once they're saved.
- No name, email or phone number is in the files. Your GitHub username will be visible, so choose a neutral one if you'd rather not use your name.

## Steps
1. **Make a free account** at github.com (only you can do this step: it needs your email and a password).
2. **Make the repository.** Click **New repository**, name it `gold-bot`, choose **Public**, and tick nothing else. Click **Create repository**.
3. **Upload the files.** On the new page, click **uploading an existing file**. From `G:\My Drive\Command Center\trading\cloud-repo`, drag in `gold_bot.py`, `journal.csv`, `state.json` and the `logs` folder. Click **Commit changes**.
4. **Add the schedule file.** Click **Add file > Create new file**. In the name box, type `.github/workflows/gold-bot.yml` (the slashes create the folders). Open that same file from the `cloud-repo\.github\workflows` folder in Notepad, copy everything into the page, and click **Commit changes**.
   (Or sign in to GitHub in Claude's browser pane and Claude will do steps 2 to 4 for you.)
5. **Add your keys** (only you can do this step). Go to **Settings > Secrets and variables > Actions > New repository secret** and add two:
   - Name `ALPACA_API_KEY`, with your paper API key ID
   - Name `ALPACA_SECRET_KEY`, with your paper secret key
   Copy them from `%USERPROFILE%\.alpaca\gold_bot.env` on the laptop. Never paste them into a chat.
6. **Test it.** Go to **Actions > Gold paper bot > Run workflow**, leave the mode on `check`, and click **Run workflow**. After about a minute, open the run. It should say `Connected to PAPER account ...` with your equity.
7. **Tell Claude it passed.** Claude will then switch off the laptop task (GoldPaperBot) so the two never trade at the same time.

## After that
- Every weekday the bot starts on its own and saves `journal.csv`, `state.json` and `logs/` back into the repository.
- To see results: open the repository and click `journal.csv`, or ask Claude for a status.
- If GitHub ever emails "workflow disabled": scheduled runs pause after 60 days with no repository activity. The bot's own daily saves count as activity, so this shouldn't happen. If it does, click **Enable workflow** on the Actions tab.
- GitHub can start scheduled runs 5 to 15 minutes late on busy days. The 06:45 AST start leaves at least 15 minutes of slack in summer and 75 in winter before the London window.
