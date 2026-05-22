# Come Aggiungere una Nuova Distribuzione

Benvenuto! Questa guida ti aiuterà ad aggiungere il supporto per una nuova distribuzione Linux o BSD a FreedomNAS.

## Struttura di un Profilo OS

Ogni distribuzione è definita da un file JSON che contiene i comandi nativi per le operazioni comuni. Il motore universal di FreedomNAS traduce le azioni astratte ai comandi specifici del tuo OS.

## Passo 1: Creare il File di Profilo

1. Crea un nuovo file `{distro_name}.json` nella directory `freedomnas-profiles/`
2. Usa il template minimo sottostante come punto di partenza

### Template Minimo

```json
{
  "os_name": "Your Distribution Name",
  "os_family": "linux",  // o "bsd"
  "os_version": "version_info",

  "users": {
    "create": "command_to_create_user",
    "delete": "command_to_delete_user",
    "set_password": "command_to_set_password",
    "list": "command_to_list_users",
    "get_user_info": "command_to_get_user_info",
    "add_to_group": "command_to_add_user_to_group",
    "remove_from_group": "command_to_remove_user_from_group"
  },

  "services": {
    "start": "command_to_start_service",
    "stop": "command_to_stop_service",
    "restart": "command_to_restart_service",
    "enable": "command_to_enable_service",
    "disable": "command_to_disable_service",
    "status": "command_to_check_service_status",
    "is_enabled": "command_to_check_if_service_is_enabled",
    "reload": "command_to_reload_service",
    "daemon_reload": "command_to_reload_daemon_config"
  },

  "packages": {
    "update_cache": "command_to_update_package_cache",
    "install": "command_to_install_package",
    "remove": "command_to_remove_package",
    "upgrade": "command_to_upgrade_all_packages"
  },

  "zfs": {
    "pool_create": "zpool command syntax for your OS",
    "pool_import": "zpool import command",
    "pool_list": "zpool list command"
  },

  "system": {
    "hostname_set": "command_to_set_hostname",
    "reboot": "command_to_reboot_system",
    "shutdown": "command_to_shutdown_system",
    "uptime": "command_to_check_uptime",
    "uname": "command_to_get_system_info"
  }
}
```

## Passo 2: Completare le Categorie

Aggiungi il supporto per le seguenti categorie (se rilevanti per la tua distribuzione):

- **network**: Comandi per configurare la rete
- **samba**: Comandi per SMB/CIFS
- **nfs**: Comandi per NFS
- **btrfs**: Comandi per il filesystem Btrfs (se supportato)
- **grub_btrfs**: Comandi per Grub con snapshot Btrfs

## Passo 3: Placeholder dei Parametri

Usa i placeholder in formato `{parameter_name}` nei comandi. Esempi comuni:

- `{service_name}`: Nome del servizio
- `{package_name}`: Nome del pacchetto
- `{username}`: Nome utente
- `{groups}`: Lista di gruppi
- `{hostname}`: Nome host
- `{ip}`: Indirizzo IP
- `{interface}`: Nome interfaccia di rete
- `{pool_name}`: Nome pool ZFS
- `{dataset}`: Dataset ZFS

## Passo 4: Testare il Profilo

### Test Manuale

```python
from freedomnas_core.engine import FreedomNASEngine

# Creare l'engine con il nuovo profilo
engine = FreedomNASEngine(os_override="your_distro")

# Testare alcuni comandi
cmd = engine.get_command("services", "start", service_name="samba")
print(f"Start Samba: {cmd}")

cmd = engine.get_command("packages", "install", package_name="vim")
print(f"Install vim: {cmd}")

# Verificare informazioni OS
print(engine.get_os_info())
```

### Esecuzione Reale

```python
# Per sistemi di produzione, testare con esecuzione effettiva
try:
    stdout, stderr = engine.execute("packages", "update_cache", timeout=60)
    print(f"Update cache completed")
except Exception as e:
    print(f"Error: {e}")
```

## Passo 5: Submittere il Contributo

1. Verifica che il tuo profilo JSON sia valido:
   ```bash
   python -m json.tool {distro_name}.json
   ```

2. Aggiungi un commento nel file JSON o in un README specifico per la tua distro

3. Crea una Pull Request con:
   - Il nuovo file `{distro_name}.json`
   - Un commento nel `CONTRIBUTING.md` con link a dove testare
   - Test case di esempio (opzionale ma molto apprezzato)

## Linee Guida per i Comandi

### Sicurezza

- **NON usare la shell** a meno che assolutamente necessario
- Usa parametri di linea di comando, non pipe shell
- Evita l'interpolazione di stringhe non sicure
- Preferiamo comandi che falliscono chiaramente rispetto a quelli che lo fanno silenziosamente 

### Compatibilità

- Usa comandi che esistono nella versione minima supportata della distro
- Se un comando cambia tra versioni, usa quello della versione più stabile
- Documenta i requisiti di versione nel commento

### Formato

- Un comando per azione
- Se necessari comandi multipli, usa un array JSON: `["cmd1", "cmd2"]`
- Mantieni la coerenza con i nomi delle azioni negli altri profili

## Esempi

### Linux Generico

```json
"packages": {
  "update_cache": "apt-get update",
  "install": "apt-get install -y {package_name}",
  "remove": "apt-get remove -y {package_name}",
  "upgrade": "apt-get dist-upgrade -y"
}
```

### BSD

```json
"packages": {
  "update_cache": "pkg update",
  "install": "pkg install -y {package_name}",
  "remove": "pkg remove -y {package_name}",
  "upgrade": "pkg upgrade -y"
}
```

### Comandi Multipli

```json
"zfs": {
  "pool_create": [
    "zpool create -o ashift=12 {pool_name} {vdev_type}",
    "zfs set recordsize=1M {pool_name}"
  ]
}
```

## Supporto

- **Domande?** Apri una discussione nel repository
- **Errori nel profilo?** Segnala un issue con il comando che fallisce
- **Miglioramenti?** Sentiamoci per coordinare le modifiche

Grazie per il tuo contributo a FreedomNAS! 🎉
