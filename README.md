# AI & Markets Telegram Bot

News alerts (scored by relevance), price context, interactive charts and daily briefings for AI-industry stocks, the S&P 500 and macro events.

## GitHub Actions deploy
1. Put ALL files of this folder at the ROOT of your repo (bot.py must sit next to config.yaml, and `.github/workflows/bot.yml` must exist).
2. Settings -> Secrets and variables -> Actions: add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
3. Settings -> Actions -> General -> Workflow permissions -> Read and write.
4. Actions -> market-bot -> Run workflow. You should receive "Bot is live".

## Local run
```bash
pip install -r requirements.txt
cp .env.example .env            # fill in token
python bot.py --get-chat-id     # after sending /start to your bot
python bot.py --test
python bot.py --loop
python -m unittest discover -s tests -v
```
Run EITHER GitHub Actions OR a local/server loop, never both. Tune everything in config.yaml. Not financial advice.
