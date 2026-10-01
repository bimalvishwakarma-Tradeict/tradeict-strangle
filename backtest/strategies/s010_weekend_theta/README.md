## S010 Weekend Theta — status: RESULTS ON HOLD (bug)

### Structure
Entry D 18:00 IST. Lot = 0.001 BTC. Ratio 1:1:2 (base = short straddle qty).
Backtest qty 1000/1000/2000.
- Arm A: SHORT D+2 ATM straddle + SHORT D+2 OTM strangle, LONG D+3 ATM straddle
- Arm B (control): wahi shorts, LONG **D+2** ATM straddle (same expiry, no calendar)
- Strangle strikes: call pehle (highest K <= upper breakeven), phir put
  premium-match (call ke mark ke sabse nazdeek)
- Exit: D+2 17:30. D+2 legs expire free. Arm A sirf D+3 longs close karta hai.

### Data window
DATA_START = 2025-01-01. 2024 ka data JAAN BOOJH KAR chhoda hai --
us saal D+3 chain sirf 66.8% din listed thi (2025/2026 mein 97.8%).
Ise mat badalna bina chain availability dobara naape.

### Run 1 result (commit d931c58, 2026-10-01) -- BHAROSA MAT KARO
5/612 arm A baskets ne structural cap toda. Wajah: `_ffill_series` exit par
missing mark ko entry premium se bhar deta hai, aur `no_exit_mark` sirf
value <= 0 par skip karta hai. Hedge entry price par jam jata hai jabki
shorts poora intrinsic chuka dete hain.
Misal 2025-02-26: spot 88298 -> 80362, put intrinsic 7838, engine ne 1396
(entry value) use kiya. Reported loss -10488, structural cap -2233.

### Jo is run se phir bhi padha ja sakta hai
- Target/stop +/-10% of capital KAAM NAHI KARTA. arm B tsON ka win% har din
  0-1.1%. Band position ke apne jhoole se chhota hai. tsOFF har jagah behtar.
- Arm B ka gross 7 mein se 6 din minus hai. Sirf Saturday +90.74, par uska
  net -25.81 aur CI zero cross karta hai.

### Chetavni -- 5 violations problem ka naap NAHI hai
Cap invariant sirf us galti ko pakadta hai jo result ko BURA dikhaye.
Shaant dinon par wahi stale mark hedge ko ZYADA aankta hai aur jhootha
munafa banata hai, jise koi invariant nahi pakadta.

### Fix abhi tay nahi
- Intrinsic floor: GALAT. Exit par option ke paas 1 din bacha hai, to
  intrinsic par floor lagana hedge ko systematically kam aankna hai.
- Skip: behtar, par neutral nahi -- missing chain bade-move waale dinon se
  juda hai, to skip karne se wahi din hat jayenge jo structure ka imtihaan
  lete hain. Agar skip karein to skipped dinon ka realized move bhi report
  karna hoga.
- Nazdeek ka asli mark: sabse accha, agar milta ho. Audit se pata chalega.
