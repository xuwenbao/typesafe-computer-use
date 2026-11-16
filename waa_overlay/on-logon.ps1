$scriptFolder = "\\host.lan\Data"
$pythonServerPort = 5000

# Start the Caddy reverse proxy in a non-blocking manner
Write-Host "Running the Caddy reverse proxy from port 9222 to port 1337"
Start-Process -NoNewWindow -FilePath "powershell" -ArgumentList "-Command", "caddy reverse-proxy --from :9222 --to :1337"

# jev: a windowless restarter. close_all kills a server that has an ordinary console title.
$pythonw = "C:\Users\Docker\AppData\Local\Programs\Python\Python310\pythonw.exe"
Write-Host "Running the WinArena server on port $pythonServerPort..."
Start-Process -WindowStyle Hidden -FilePath $pythonw -ArgumentList "`"$scriptFolder\keep_server.py`""

# jev: the Clock app otherwise sits on the Store's "Getting updates ready" dialog.
$policy = "HKLM:\SOFTWARE\Policies\Microsoft\WindowsStore"
New-Item -Path $policy -Force | Out-Null
New-ItemProperty -Path $policy -Name AutoDownload -Value 2 -PropertyType DWord -Force | Out-Null
$delivery = "HKCU:\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager"
New-Item -Path $delivery -Force | Out-Null
New-ItemProperty -Path $delivery -Name SilentInstalledAppsEnabled -Value 0 -PropertyType DWord -Force | Out-Null
