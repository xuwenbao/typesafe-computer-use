#!/bin/bash

echo "Starting WinArena VM..."

# Start the VM script in the background
cd / # Fix for Azure ML Job not using the correct root path
./start_vm.sh &

# Wait for the VM to start up. A dropped SYN must time out, or the first probe waits forever.
while true; do
  response=$(curl --connect-timeout 5 --max-time 10 --write-out '%{http_code}' --silent --output /dev/null 20.20.20.21:5000/probe)

  if [ "$response" = "200" ]; then
    break
  fi

  echo "Waiting for a response from the windows server. This might take a while..."

  sleep 5
done

echo "VM is up and running, and the Windows Arena Server is ready to use!"
