# Rommer · R36S Sync

Rommer copies files that are already on your computer to an R36S ROM folder. It follows the local collection copy workflow described by [RomGoGetter](https://github.com/shokoe/RomGoGetter), with an R36S destination and a preview-first sync screen.

## Run

On Windows with Python 3.10 or newer:

```powershell
py r36s_sync.py
```

## Use

1. Choose the local collection folder.
2. Choose the R36S ROMs folder on the SD card, commonly the `EASYROMS` partition root, or enter a mounted network share such as `\\R36S\roms`.
3. Scan and review the preview.
4. Start the sync.

The source folder's relative paths are preserved at the destination. For a collection already split into system folders, choose its parent as the source and the R36S ROMs root as the destination. For one system, choose its folder and the matching system folder on the device.

The sync copies existing local files only. It skips files whose size and modified time match, replaces changed files, and never deletes files from the device. It does not fetch content from websites or alter the R36S operating system partition.

For ArkOS, its documentation identifies the ROM folders as the `EASYROMS` partition when the SD card is attached to a computer and `/roms` when accessed over the network: [ArkOS folder information](https://github.com/christianhaitian/arkos/wiki/ArkOS-Emulators-and-Ports-information).
