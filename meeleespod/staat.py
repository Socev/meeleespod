"""Opslag op app-data (/staat): kleine JSON-bestanden, 0600, atomair geschreven. Nooit tekst of bijlagen."""
import json
import os
import secrets
import threading

from . import instellingen as I

_slot = threading.RLock()


def _pad(naam):
    if not naam.replace("_", "").replace("-", "").isalnum():
        raise ValueError("ongeldige naam")
    return I.STAAT / f"{naam}.json"


def lees(naam, standaard=None):
    with _slot:
        try:
            with open(_pad(naam), encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {} if standaard is None else standaard
        except (OSError, ValueError):
            return {} if standaard is None else standaard


def schrijf(naam, data):
    with _slot:
        I.STAAT.mkdir(parents=True, exist_ok=True, mode=0o700)
        doel = _pad(naam)
        tmp = doel.with_name(f".{doel.name}.{secrets.token_hex(6)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, doel)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def werk_bij(naam, functie, standaard=None):
    """Lezen, aanpassen en terugschrijven onder één slot. functie(data) past data ter plekke aan."""
    with _slot:
        d = lees(naam, standaard)
        uit = functie(d)
        schrijf(naam, d)
        return uit
