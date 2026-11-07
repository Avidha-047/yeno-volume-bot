# Submission – Fair-Value Volume Bot

| | |
|---|---|
| Bot name | Fair-Value Volume Bot |
| Builder / contact | Avidha Chandra |
| Repository | https://github.com/Avidha-047/yeno-volume-bot |
| Run command | `python bot.py` – serves `POST /decide` on `127.0.0.1:8080` (`HOST` / `PORT` environment variables override) |
| Dependencies | none – Python 3.9+ standard library only |
| Network | none – the bot makes no outbound requests; it decides only from the evaluator's request |
| Contract check | `python yeno-smoke-test.py` → `"contractSmokePassed": true` |
| Tests | `python -m unittest tests.test_all` |
| Decision time | < 1 ms |
| Markets | YeNo BTC five-minute Up/Down only |

Strategy, evidence and limits: see `README.md`. The bot holds no state across process restarts that affects
correctness; a restart only resets its volatility estimate and pacing clock.
