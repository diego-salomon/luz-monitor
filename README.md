# Luz monitor

A monthly check of your electricity bill in Spain. It downloads your hourly consumption from Datadis, prices it against the regulated tariff (PVPC, from Red Eléctrica), your contract and the offers you list, and sends a report: what each option would have cost over the last 12 months, the contracted power you really need, and alerts for anything that changed without you asking (supplier, power, estimated readings, rising night consumption, upcoming renewal).

Python standard library only. Passwords live in the macOS Keychain.

## Run it

Try it with made-up data first (needs internet for PVPC prices):

```bash
cp config.example.json config.json
python3 luzgas.py --demo --sin-correo --abrir
```

With your data:

1. Create an account at [datadis.es](https://datadis.es).
2. Fill in `config.json`: NIF/NIE, CUPS (on your bill), your prices and the offers to compare.
3. `./guardar_contrasena.sh luzgas-datadis YOUR_NIF "Datadis"`
4. `python3 luzgas.py`

For email, store a Gmail app password with `./guardar_contrasena.sh luzgas-smtp you@gmail.com "Gmail"`, set `"metodo": "smtp"` in the `email` block, and schedule it with `./instalar.sh`. `config.json`, the cache and the reports are git-ignored.

The report and code comments are in Spanish. MIT license.
