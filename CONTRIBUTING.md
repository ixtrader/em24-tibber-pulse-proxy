# Contributing

Issues and pull requests are welcome.

Before opening a pull request:

1. Keep changes focused and preserve the EM24 register compatibility required by Victron.
2. Do not commit `.env`, passwords, bridge addresses, or other private configuration.
3. Run the local checks:

   ```bash
   python3 -m py_compile EM24-Tibber-Pulse-Proxy.py tools/em24_dump.py
   ./compose.sh config
   ./compose.sh build
   ```

For behaviour changes, include the relevant `tools/em24_dump.py` output or a concise explanation of the tested Modbus registers.
