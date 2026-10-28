# Luz Monitor

A script that checks my electricity bill so I don't have to. Once a month it downloads my hourly consumption from Datadis, Spain's official metering platform. It prices every hour against the regulated tariff (PVPC, from Red Eléctrica), against my current contract and against a list of offers, and emails me a short report.

## What the report tells me

- What the last 12 months would have cost on my tariff, on PVPC and on each offer
- The contracted power I actually need, based on my real peaks
- Alerts: the supplier changed without my asking, the power changed, I went over my contracted power, readings were estimated, night consumption is creeping up, the contract renews soon
- What to do next, step by step, with a pre-filled link to a public comparison tool

## Details I'm happy with

- **Python standard library only.** Nothing to install.
- **No secrets on disk.** The Datadis password and the optional SMTP password live in the macOS Keychain. `guardar_contrasena.sh` stores them through a native dialog.
- **Handles bad data.** When Datadis started returning duplicated days, the script switched to the real meter readings from my bills. It keeps the totals exact and spreads them across the hours.
- **Runs itself**, from a scheduled Claude task or from launchd (`instalar.sh`).

## Setup

```bash
cp config.example.json config.json   # add your NIF/NIE, CUPS and current tariff
./guardar_contrasena.sh luzgas-datadis YOUR_NIF "Datadis"
/usr/bin/python3 luzgas.py
```

The code comments are in Spanish.
