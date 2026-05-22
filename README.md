# FreedomNAS Profiles

I file di traduzione OS specifici per FreedomNAS.

## Profili Disponibili

- **debian.json**: Debian, Ubuntu, basati su Debian
- **freebsd.json**: FreeBSD 13.0+
- **arch.json**: Arch Linux (community-maintained)

## Cosa Sono i Profili?

Ogni profilo è un file JSON che mappa azioni astratte ai comandi nativi del sistema operativo.

**Esempio:**

```json
"services": {
  "start": "systemctl start {service_name}",  // Debian
  "stop": "systemctl stop {service_name}"
}
```

Su FreeBSD lo stesso file sarebbe:
```json
"services": {
  "start": "service {service_name} start",
  "stop": "service {service_name} stop"
}
```

## Come il Core Engine Usa i Profili

1. Rileva il sistema operativo
2. Carica il profilo corrispondente
3. Traduce le azioni astratte ai comandi nativi
4. Esegue i comandi in sicurezza

```python
from freedomnas_core.engine import FreedomNASEngine

engine = FreedomNASEngine()

# Su Debian: systemctl restart samba
# Su FreeBSD: service samba restart
# Su Arch: systemctl restart samba
engine.execute("services", "restart", service_name="samba")
```

## Aggiungere una Nuova Distribuzione

Vedi [CONTRIBUTING.md](CONTRIBUTING.md) per una guida completa.

## Struttura di un Profilo

```json
{
  "os_name": "Distribution Name",
  "os_family": "linux|bsd",
  "os_version": "version_spec",
  
  "users": { /* user management commands */ },
  "services": { /* service management commands */ },
  "network": { /* network configuration commands */ },
  "packages": { /* package manager commands */ },
  "zfs": { /* ZFS commands */ },
  "samba": { /* Samba/SMB commands */ },
  "nfs": { /* NFS commands */ },
  "system": { /* system info/control */ }
}
```

Vedi i file JSON per esempi completi di ogni categoria.
