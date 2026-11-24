#Requires -RunAsAdministrator
<#
    fix_adb_winusb.ps1   (ASCII-only on purpose: safe for Windows PowerShell 5.1)

    Problem
    -------
    On Windows, `adb` only enumerates USB devices whose ADB interface has
    registered the Android ADB device-interface GUID
    {F72FE0D4-CBCB-407D-8814-9ED673D0DD6B}.
    When Windows auto-binds the generic "WinUsb Device" driver
    (compatible id USB\MS_COMP_WINUSB) to the ADB interface, that GUID is
    never registered, so `adb devices` stays empty while Device Manager and
    Explorer still show the phone.

    Fix
    ---
    Append the adb GUID to the "DeviceInterfaceGUIDs" (REG_MULTI_SZ) value in
    the device instance's "Device Parameters" key, then restart that device so
    Windows re-registers its device interfaces.

    Usage (Administrator PowerShell)
    -------------------------------
        cd d:\projects\DeleteMemory\operate
        powershell -ExecutionPolicy Bypass -File .\fix_adb_winusb.ps1

    Verify only (no changes):
        powershell -ExecutionPolicy Bypass -File .\fix_adb_winusb.ps1 -VerifyOnly

    Rollback:
        powershell -ExecutionPolicy Bypass -File .\fix_adb_winusb.ps1 -Rollback
#>
[CmdletBinding()]
param(
    [switch]$Rollback,
    [switch]$VerifyOnly
)

$ErrorActionPreference = 'Stop'
$adbGuid    = '{F72FE0D4-CBCB-407D-8814-9ED673D0DD6B}'
$enumRoot   = 'HKLM:\SYSTEM\CurrentControlSet\Enum'
$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$backupFile = Join-Path $scriptDir 'adb_winusb_backup.reg'

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $pr = New-Object Security.Principal.WindowsPrincipal $id
    return $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-AdbInterfaceDevices {
    $found = New-Object System.Collections.ArrayList
    foreach ($dev in (Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue)) {
        if ($dev.InstanceId -notlike 'USB\VID_*') { continue }
        $compat = (Get-PnpDeviceProperty -InstanceId $dev.InstanceId `
                     -KeyName 'DEVPKEY_Device_CompatibleIds' -ErrorAction SilentlyContinue).Data
        if (-not $compat) { continue }
        $joined = ($compat -join '|')
        # class 0xff / subclass 0x42 / protocol 0x01 == Android ADB interface
        if ($joined -like '*Class_ff*SubClass_42*Prot_01*') {
            [void]$found.Add($dev)
        }
    }
    return $found
}

function Show-HasAdbGuid {
    param([string]$RegPath)
    $val = (Get-ItemProperty -Path $RegPath -Name 'DeviceInterfaceGUIDs' -ErrorAction SilentlyContinue).DeviceInterfaceGUIDs
    if ($val -and (@($val) -contains $adbGuid)) {
        Write-Host '    DeviceInterfaceGUIDs : adb GUID PRESENT' -ForegroundColor Green
        return $true
    }
    Write-Host '    DeviceInterfaceGUIDs : adb GUID MISSING' -ForegroundColor Yellow
    return $false
}

Write-Host ''
Write-Host '=== adb WinUSB interface fix ===' -ForegroundColor Cyan

if (-not (Test-IsAdmin)) {
    Write-Host 'ERROR: administrator rights required.' -ForegroundColor Red
    Write-Host 'Right-click PowerShell -> Run as administrator, then run this script again.'
    exit 1
}

$devices = Get-AdbInterfaceDevices
if (-not $devices -or $devices.Count -eq 0) {
    Write-Host 'No Android ADB interface found (class ff/42/01).' -ForegroundColor Yellow
    Write-Host 'Check:  (1) Developer options / USB debugging is ON'
    Write-Host '        (2) Data cable is connected'
    Write-Host '        (3) Phone is NOT set to "Charging only if it hides ADB"'
    exit 2
}

$allOk = $true

foreach ($dev in $devices) {
    $inst    = $dev.InstanceId
    $regPath = "$enumRoot\$inst\Device Parameters"

    Write-Host ''
    Write-Host "--- $($dev.FriendlyName) ---"
    Write-Host "    InstanceId : $inst"
    Write-Host "    Status     : $($dev.Status)"

    if (-not (Test-Path $regPath)) {
        Write-Host "    ERROR: registry path not found: $regPath" -ForegroundColor Red
        $allOk = $false
        continue
    }

    if ($VerifyOnly) {
        [void](Show-HasAdbGuid -RegPath $regPath)
        continue
    }

    $existing = @()
    $prop = Get-ItemProperty -Path $regPath -Name 'DeviceInterfaceGUIDs' -ErrorAction SilentlyContinue
    if ($prop) { $existing = @($prop.DeviceInterfaceGUIDs) }

    if ($Rollback) {
        if (@($existing) -contains $adbGuid) {
            $left = @($existing | Where-Object { $_ -and $_ -ne $adbGuid })
            if ($left.Count -eq 0) {
                Remove-ItemProperty -Path $regPath -Name 'DeviceInterfaceGUIDs' -ErrorAction SilentlyContinue
            } else {
                Set-ItemProperty -Path $regPath -Name 'DeviceInterfaceGUIDs' -Value ([string[]]$left)
            }
            Write-Host '    Removed adb GUID from DeviceInterfaceGUIDs' -ForegroundColor Green
            $changed = $true
        } else {
            Write-Host '    adb GUID not present, nothing to roll back'
            $changed = $false
        }
    } else {
        if (@($existing) -contains $adbGuid) {
            Write-Host '    adb GUID already present, only restarting device'
            $changed = $true
        } else {
            # one-time backup of the current values
            if (-not (Test-Path $backupFile)) {
                try {
                    # go through cmd so native stderr never becomes a terminating error
                    & cmd.exe /c "reg export `"HKLM\SYSTEM\CurrentControlSet\Enum\$inst\Device Parameters`" `"$backupFile`" /y >nul 2>&1"
                    Write-Host "    Backup written: $backupFile" -ForegroundColor DarkGray
                } catch {
                    Write-Host '    WARN: registry backup failed (continuing)' -ForegroundColor Yellow
                }
            }
            $newValue = [string[]](@($existing) + $adbGuid | Where-Object { $_ })
            New-ItemProperty -Path $regPath -Name 'DeviceInterfaceGUIDs' `
                -PropertyType MultiString -Value $newValue -Force | Out-Null
            Write-Host "    Wrote DeviceInterfaceGUIDs = $adbGuid" -ForegroundColor Green
            $changed = $true
        }
    }

    if ($changed) {
        Write-Host '    Restarting device to re-enumerate interfaces...' -ForegroundColor Cyan
        $restarted = $false
        & cmd.exe /c "pnputil /restart-device `"$inst`" >nul 2>&1"
        if ($LASTEXITCODE -eq 0) { $restarted = $true }
        if (-not $restarted) {
            try {
                Disable-PnpDevice -InstanceId $inst -Confirm:$false -ErrorAction Stop
                Start-Sleep -Seconds 2
                Enable-PnpDevice  -InstanceId $inst -Confirm:$false -ErrorAction Stop
                $restarted = $true
            } catch {
                Write-Host '    WARN: automatic restart failed. Please unplug and replug the cable.' -ForegroundColor Yellow
            }
        }
        if ($restarted) { Write-Host '    Device restarted OK' -ForegroundColor Green }
        Start-Sleep -Seconds 2
    }

    if (-not $Rollback) {
        if (-not (Show-HasAdbGuid -RegPath $regPath)) { $allOk = $false }
    }
}

if ($VerifyOnly) {
    Write-Host ''
    Write-Host 'Verify mode finished, nothing was changed.' -ForegroundColor Cyan
    exit 0
}

Write-Host ''
Write-Host '=== adb check ===' -ForegroundColor Cyan

# adb writes its daemon banner to stderr; with EAP=Stop that would become a
# terminating NativeCommandError, so capture through cmd instead.
$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& cmd.exe /c 'adb kill-server >nul 2>&1' | Out-Null
Start-Sleep -Seconds 1
$devOut = @(& cmd.exe /c 'adb devices -l 2>&1' | ForEach-Object { "$_" })
$ErrorActionPreference = $prevEap

foreach ($line in $devOut) { Write-Host $line }

$serialLines = @($devOut | Where-Object { $_ -match '\s(device|unauthorized|offline)\s*$' })
if ($serialLines.Count -gt 0) {
    Write-Host ''
    Write-Host "SUCCESS: adb sees $($serialLines.Count) device(s)." -ForegroundColor Green
    Write-Host 'If the state is "unauthorized", unlock the phone and tap Allow.' -ForegroundColor Yellow
    exit 0
}

Write-Host ''
Write-Host 'STILL EMPTY: adb does not see the device yet.' -ForegroundColor Red
Write-Host 'Next steps:'
Write-Host '  1) Unplug and replug the USB cable, then run: adb devices'
Write-Host '  2) Try another USB port (prefer a rear motherboard port, no hub).'
Write-Host '  3) If still failing, install the official vendor ADB driver (Honor/HiSuite or Google USB Driver).'
if ($allOk) { exit 3 } else { exit 4 }
