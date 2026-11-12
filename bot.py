"""Punto d'ingresso del bot (usato da systemd e da main.py).

Il codice vive nel pacchetto `deal_hunter`; questo file resta nella radice
perché il servizio systemd esegue `python3 bot.py` da questa cartella.
"""

from deal_hunter.bot.app import main

if __name__ == "__main__":
    main()
