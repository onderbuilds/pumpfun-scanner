# pumpfun-scanner

Streams new pump.fun tokens on Solana from free public RPCs, checks each mint (authorities, Token-2022 extensions, supply) and runs a paper-trading simulation on top. No wallet, no private keys, no paid APIs. It does not trade.

## Files

- `collector_v2.py` – websocket collector with redundant RPC connections, writes hourly jsonl to `data/`
- `pumpdecode.py`, `mintlib.py` – transaction decoding and mint checks
- `analyze.py` – simulation over collected data, outputs to `results/`
- `paper_bot.py`, `entry_gate.py`, `risk.py` – paper bot with daily loss breaker and per-trade cap
- `fee_from_blocks.py` – creator fee estimates per token
- `config.json` – bankroll, risk limits, fee constants

## Usage

```sh
pip install websockets requests pandas
./run_collector.sh        # keeps the collector running
python3 analyze.py
./run_paper_bot.sh
```

## Tests

```sh
python3 -m unittest discover -s tests
```

## License

MIT
