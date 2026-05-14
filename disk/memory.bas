10 POKE 49152, 1 : REM Switch to Bank 1
20 POKE 32768, 99 : REM Write to Window
30 POKE 49152, 2 : REM Switch to Bank 2
40 PRINT PEEK(32768) : REM Should be 0
50 POKE 49152, 1 : REM Back to Bank 1
60 PRINT PEEK(32768) : REM Should be 99