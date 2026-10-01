## WARNING -- forward-filled marks (2026-10-01)
`_ffill_series` missing mark ko aakhri known value se bharta hai. Agar koi
engine "missing" ko sirf `value <= 0` se pehchanta hai, to poori tarah gayab
mark chupchaap purani keemat par trade ho jayega.
Har call site par price ka SOURCE tag karo: real / near / stale / none.
`> 0` ko "present" ka matlab mat maano.
S010 mein isne ek basket par -10488 ka jhootha nuksan banaya (asli cap -2233).
JAANCH BAKI: kya S001 mark engine, S006, S008 bhi yahi helper use karte hain.
