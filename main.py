import os
from dotenv import load_dotenv
import subprocess

# Carica variabili d'ambiente
load_dotenv()

if __name__ == "__main__":
    if not os.getenv("TELEGRAM_TOKEN"):
        print("Errore: TELEGRAM_TOKEN non trovato nel file .env")
        print("Crea un file .env con: TELEGRAM_TOKEN=tuo_token")
    else:
        print("Avvio del Deal Hunter Bot...")
        # Esegue il bot usando il venv
        subprocess.run(["./venv/bin/python3", "bot.py"])
