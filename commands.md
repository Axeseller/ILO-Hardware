Terminal: you type commands:
pio run (build)
pio run -t upload (flash)
pio device monitor (serial output)
pio run -t clean
After a day or two you’ll have these in muscle memory or shell history, and you can chain them: pio run -t upload && pio device monitor.