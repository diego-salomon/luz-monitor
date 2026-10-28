# Luz monitor

A script that checks my electricity bill so I don't have to. Once a month it downloads my hourly consumption from Datadis, the platform where Spanish distributors publish meter data. It prices every hour against the regulated tariff (PVPC, published by Red Eléctrica), against my contract and against a short list of offers I keep in the config, and emails me a report.

The report says what the last 12 months would have cost on each option, what contracted power I really need based on my peaks, and what to do next. It also raises alerts: a supplier change I didn't ask for, a change in contracted power, going over that power, estimated readings, night consumption creeping up, and a renewal coming soon.

It only uses the Python standard library, so there's nothing to install. Passwords live in the macOS Keychain, never in a file. When Datadis started returning duplicated days, I made it fall back to the real meter readings from my bills: the totals stay exact and are spread across the hours.

## Run it

Try it first with made-up data. It doesn't need a Datadis account, only an internet connection to download PVPC prices:

```bash
cp config.example.json config.json
python3 luzgas.py --demo --sin-correo --abrir
```

To use your own data:

1. Create an account at [datadis.es](https://datadis.es) with your NIF or NIE.
2. Fill in `config.json`: your NIF/NIE, your CUPS (on your bill), your current prices and the offers you want to compare.
3. Save the Datadis password in the Keychain: `./guardar_contrasena.sh luzgas-datadis YOUR_NIF "Datadis"`
4. Run `python3 luzgas.py`.

To get the report by email, add a Gmail app password with `./guardar_contrasena.sh luzgas-smtp you@gmail.com "Gmail"`, set `"metodo": "smtp"` inside the `email` block of the config, and run `./instalar.sh` to schedule it with launchd. `config.json`, the cache and the reports are in `.gitignore`, because they hold your personal data.

The code comments are in Spanish, and so is the report.
