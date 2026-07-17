# PACE5000 Useful SCPI Command Extract

This is a compact, experiment-oriented extract from
`GE-PACE5000-6000-COMM-MANUAL.pdf` for future PACE5000 command expansion.

Context: the instrument is used to control helium gas pressure applied to a
membrane-driven diamond anvil cell (DAC). The commands below are therefore
biased towards pressure measurement, pressure setpoint control, slew-rate
control, source-pressure checks, in-limits detection, status/error handling,
and reproducible logging.

Manual references use the manual section page numbers, for example `4-42`.

## SCPI Syntax Notes

Manual refs: `2-1` to `2-5`.

| Item | Meaning |
|---|---|
| Terminator | Commands are terminated by line feed (`\n`). |
| Case | Command headers are case-insensitive. Strings are case-sensitive. |
| Short form | Uppercase letters in the manual are the valid short form, e.g. `:OUTP` for `:OUTPut`. |
| Optional nodes | Bracketed nodes are optional, e.g. `:SOURce[:PRESsure][:LEVel][:IMMediate][:AMPLitude] <number>` can be sent as `:SOUR:PRES <number>`. |
| Boolean | Commands accept `1`/`0`; examples also use `ON`/`OFF`. Boolean queries return `1` or `0`. |
| Enumerated data | Queries return short uppercase values, e.g. `LIN`, `MAX`, `NITR`. |
| Strings | String query responses are returned in double quotes. |
| Units | Numeric pressure and pressure-rate responses are normally in the currently selected pressure unit. |

Most PACE queries echo the command header before the value, for example
`:SENS:PRES 1.2340000`. For implementation, preserve the full line for
multi-field responses such as `:SENS:PRES:INL?`, `:INST:LIM?`, `*IDN?`, and
`:SYST:ERR?`.

## Recommended Pressure-Step Sequence

For DAC membrane loading/unloading, use a conservative sequence like this:

1. `:UNIT:PRES MPA`
2. `:UNIT:PRES?`
3. `:SOUR:PRES:COMP1?`
4. Confirm the +ve source pressure is above the requested target.
5. `:SOUR:PRES:SLEW:MODE LIN`
6. `:SOUR:PRES:SLEW <rate_in_MPa_per_s>`
7. `:SOUR:PRES:SLEW?`
8. Confirm the returned slew rate.
9. `:SOUR:PRES:SLEW:OVER 0` if overshoot should be suppressed.
10. `:SOUR:PRES <target_MPa>`
11. `:OUTP:STAT 1` if active control is not already enabled.
12. Poll `:SENS:PRES?`, `:SENS:PRES:SLEW?`, and optionally `:SENS:PRES:INL?`.

The important safety rule is to set and verify the slew rate before sending a
new setpoint.

## Common Commands

| Command / query | Parameters | Response | Ref | Notes |
|---|---|---|---|---|
| `*CLS` | none | none | `4-68` | Clears all queues and event/condition registers. |
| `*ESE <mask>` | 8-bit integer, `0` to `255` | none | `4-69` | Sets the Standard Event Status Enable register. |
| `*ESE?` | none | 8-bit integer | `4-69` | Queries the Standard Event Status Enable register. |
| `*ESR?` | none | 8-bit integer | `4-70` | Reads the Standard Event Status Register. Reading clears it. |
| `*IDN?` | none | comma-separated manufacturer, model, serial number, software version | `4-71` | Example: `*IDN GE Druck,Pace5000 User Interface,58784,01.05.04`. |
| `*SRE <mask>` | 8-bit integer, `0` to `255` | none | `4-72` | Sets the Service Request Enable register. |
| `*SRE?` | none | 8-bit integer | `4-72` | Queries the Service Request Enable register. |
| `*STB?` | none | 8-bit integer | `4-73` | Reads the Status Byte. Bit `2` = error available, bit `4` = message available, bit `5` = standard event, bit `7` = operation status. |

## `:UNIT` - Pressure Units

| Command / query | Parameters | Response | Ref | Notes |
|---|---|---|---|---|
| `:UNIT:PRES <name>` | one valid unit name, e.g. `MPA`, `BAR`, `MBAR`, `KPA`, `PA`, `PSI`, `USER1` to `USER4` | none | `4-65` | Selects the current pressure unit. This affects pressure readings, setpoints, source pressures, and slew-rate values. |
| `:UNIT:PRES?` | none | selected unit name, e.g. `:UNIT:PRES MPA` | `4-65` | Query before parsing numeric pressure values. |
| `:UNIT:PRES:DEFine[x] <string>,<number>` | `x = 1` to `4`; unit name string; conversion factor from Pa to the user unit | none | `4-66` | Defines a user unit. Avoid in routine experiment scripts unless the log format is designed for it. |
| `:UNIT:PRES:DEFine[x]?` | `x = 1` to `4` | unit name string and conversion factor | `4-66` | Example response: `:UNIT:PRES:DEF4 "MyUnit", 2000.0000000`. |

Recommended convention for this project: use `:UNIT:PRES MPA` for automated DAC
control unless an experiment explicitly requires another unit.

## `:INSTrument` - Instrument Metadata and Ranges

| Command / query | Parameters | Response | Ref | Notes |
|---|---|---|---|---|
| `:INST:CAT?` | none | comma-separated quoted range strings | `4-10` | Lists ranges fitted to the instrument. Includes `"BAROMETER"` if fitted. |
| `:INST:CAT:ALL?` | none | comma-separated quoted range strings | `4-11` | Lists all fitted ranges. Use before range-selection commands. |
| `:INST:LIM[x]?` | optional `x = 1` to `4` | range string, upper full-scale, lower full-scale | `4-12` | `1` control sensor, `2` +ve source, `3` -ve source, `4` barometer. Without suffix, `x = 1`. |
| `:INST:SENS[x]:CALD[y]?` | `x = 1` to `4`; optional calibration record `y` | date fields | `4-13` | Sensor calibration date. Useful for traceable experiment logs. |
| `:INST:SENS[x]:FULL?` | optional `x = 1` to `4` | full-scale decimal | `4-14` | `1` control sensor, `2` +ve source, `3` -ve source, `4` barometer. |
| `:INST:SN?` | none | integer serial number | `4-15` | Compact instrument identifier for logs. |
| `:INST:VERS[x]?` | optional `x = 1` to `5` | quoted software version string | `4-16` | `1` main code, `2` OS build, `3` boot ROM, `4` module main code, `5` module boot ROM. |

## `:OUTPut` - Controller Output State

| Command / query | Parameters | Response | Ref | Notes |
|---|---|---|---|---|
| `:OUTP:STAT <Boolean>` | `0` off, `1` on | none | `4-17` | Turns the pressure controller off/on. `1` means active pressure control. |
| `:OUTP:STAT?` | none | `0` controller off, `1` controller on | `4-17` | Query before assuming the instrument is regulating. |
| `:OUTP:LOGic[x] <Boolean>` | `x = 1` to `3`; `0`/`OFF` or `1`/`ON` | none | `4-18` | Volt-free contact option only. Not usually relevant to pressure control. |
| `:OUTP:LOGic[x]?` | `x = 1` to `3` | `0` relay off, `1` relay on | `4-18` | Returns an error if the volt-free contact option is not installed. |

## `:SENSe` - Measurements and Measurement Settings

| Command / query | Parameters | Response | Ref | Notes |
|---|---|---|---|---|
| `:SENS:PRES?` | none | decimal pressure in current pressure units | `4-19` | Main pressure reading for logging/control feedback. |
| `:SENS:PRES:INL?` | none | current pressure, then `0` not in limits or `1` in limits | `4-20` | Multi-field response. Use with `:SOUR:PRES:INL` and `:SOUR:PRES:INL:TIME`. |
| `:SENS:PRES:SLEW?` | none | decimal pressure slew in current pressure units/s | `4-21` | Measured input-pressure rate. A constant pressure returns `0.0`. |
| `:SENS:PRES:BAR?` | none | barometric pressure in current pressure units | `4-22` | Returns zero pressure if the optional barometer is not fitted. |
| `:SENS:PRES:RANG <string>` | exact case-sensitive range string | none | `4-23` | Selects the range used for pressure reading. Does not affect the front-panel display. |
| `:SENS:PRES:RANG?` | none | quoted selected range string | `4-23` | Use range strings from `:INST:CAT:ALL?`. |
| `:SENS:PRES:RES <integer>` | resolution integer | none | `4-24` | Sets display pressure resolution. Manual example shows an error for `7`; test on the actual unit before relying on high values. |
| `:SENS:PRES:RES?` | none | integer resolution | `4-24` | Example default is `6`. |
| `:SENS:PRES:CORR:HEAD <gas>,<metres>` | `AIR` or `NITRogen`; height in metres | none | `4-25` | Head correction. Helium is not listed, so do not treat this as helium head correction without validation. |
| `:SENS:PRES:CORR:HEAD?` | none | gas enum and height in metres | `4-25` | Example response includes `AIR` or `NITR`. |
| `:SENS:PRES:CORR:HEAD:STAT <Boolean>` | `0` disable, `1` enable | none | `4-26` | Enables/disables head correction. |
| `:SENS:PRES:CORR:HEAD:STAT?` | none | `0` off, `1` on | `4-26` | Log this if head correction is used. |
| `:SENS:PRES:CORR:OFFS <number>` | tare offset in current pressure units | none | `4-27` | Subtracts the offset from the processed reading. Unit-dependent. |
| `:SENS:PRES:CORR:OFFS?` | none | decimal tare offset | `4-27` | Changes numerically when pressure unit changes. |
| `:SENS:PRES:CORR:OFFS:STAT <Boolean>` | `0` disable, `1` enable | none | `4-28` | Enables/disables tare offset. |
| `:SENS:PRES:CORR:OFFS:STAT?` | none | `0` off, `1` on | `4-28` | Log this if tare offset is used. |
| `:SENS:PRES:CORR:VOL?` | none | decimal volume in litres | `4-29` | Estimated connected-system volume calculated from control effort. Useful for diagnostics/leak checks. |
| `:SENS:PRES:FILT:LPAS:BAND <number>` | decimal percent full-scale, `0` to `100` | none | `4-30` | Low-pass filter response band. Step changes larger than this band bypass filtering. |
| `:SENS:PRES:FILT:LPAS:BAND?` | none | decimal percent full-scale | `4-30` | Log with pressure data if filtering is enabled. |
| `:SENS:PRES:FILT:LPAS:FREQ <number>` | decimal seconds, `0` to `20` | none | `4-31` | Despite `FREQ`, the parameter is filter averaging time/time constant in seconds. |
| `:SENS:PRES:FILT:LPAS:FREQ?` | none | decimal seconds | `4-31` | Query filter averaging time. |
| `:SENS:PRES:FILT:LPAS:STAT <Boolean>` | `0` disable, `1` enable | none | `4-32` | Enables/disables low-pass filtering. Filtering can hide short-time dynamics. |
| `:SENS:PRES:FILT:LPAS:STAT?` | none | `0` off, `1` on | `4-32` | Log this if filtering is used. |

## `:SOURce` - Pressure Output and Control Settings

| Command / query | Parameters | Response | Ref | Notes |
|---|---|---|---|---|
| `:SOUR:PRES:COMP[x]?` | optional `x = 1` or `2` | decimal source pressure in current units | `4-34` | `1` = +ve source, `2` = -ve source. Suffix greater than `2` reports `-114,"Header suffix out of range"`. |
| `:SOUR:PRES:EFF?` | none | decimal controller effort percent, normally `-100` to `+100` | `4-35` | Positive means supply valve effort dominates; negative means vacuum valve effort dominates. Returns `0.0` when controller is off. |
| `:SOUR:PRES:INL <number>` | decimal percent full-scale, `0` to `100` | none | `4-36` | Sets the in-limits band. Manual default is `0.01 %FS`. |
| `:SOUR:PRES:INL?` | none | decimal percent full-scale | `4-36` | Query the in-limits band. |
| `:SOUR:PRES:INL:TIME <seconds>` | integer seconds, `2` to `999` | none | `4-37` | Sets how long pressure must stay in-limits before generating an in-limits indication/event. |
| `:SOUR:PRES:INL:TIME?` | none | integer seconds | `4-37` | Query in-limits dwell time. |
| `:SOUR:PRES <number>` | setpoint in current pressure units | none | `4-38` | Short form of `:SOURce[:PRESsure][:LEVel][:IMMediate][:AMPLitude]`. |
| `:SOUR:PRES?` | none | decimal setpoint in current pressure units | `4-38` | Manual example response: `:SOUR:PRES:LEV:IMM:AMPL 0.5000000`. |
| `:SOUR:PRES:LEV:IMM:AMPL:VENT <integer>` | `0` abort vent, `1` start vent | none | `4-39` | Vents the user system. Keep out of routine automated pressure steps. |
| `:SOUR:PRES:LEV:IMM:AMPL:VENT?` | none | `0` vent OK/not in progress, `1` in progress, `2` completed | `4-39` | Poll after starting a vent. |
| `:SOUR:PRES:RANG <string>` | exact case-sensitive range string | none | `4-40` | Selects the pressure-control range and changes the front-panel range display. |
| `:SOUR:PRES:RANG?` | none | quoted selected control range string | `4-40` | Use range strings from `:INST:CAT:ALL?`. |
| `:SOUR:PRES:SLEW <number>` | decimal pressure units/s | none | `4-42` | Sets controller slew rate when slew mode is `LIN`. Manual also shows `MAX` and `MIN`. |
| `:SOUR:PRES:SLEW?` | none | decimal pressure units/s | `4-42` | Read back and verify before setpoint changes. |
| `:SOUR:PRES:SLEW:MODE <mode>` | `MAXimum` or `LINear` | none | `4-43` | `MAX` approaches setpoint as quickly as possible. `LIN` uses the selected slew value. |
| `:SOUR:PRES:SLEW:MODE?` | none | `MAX` or `LIN` | `4-43` | For DAC work, verify `LIN` before controlled ramps. |
| `:SOUR:PRES:SLEW:OVER <Boolean>` | `0` overshoot not allowed, `1` overshoot allowed | none | `4-44` | `0` slows near the setpoint to avoid overshoot. Manual default is `1`. |
| `:SOUR:PRES:SLEW:OVER?` | none | `0` overshoot not allowed, `1` overshoot allowed | `4-44` | Conservative DAC starting point is usually `0`. |

## `:STATus` - Operation and Pressure Events

| Command / query | Parameters | Response | Ref | Notes |
|---|---|---|---|---|
| `:STAT:OPER:COND?` | none | 16-bit integer | `4-45` | Reads the operation condition register. |
| `:STAT:OPER:ENAB <mask>` | 16-bit integer, `0` to `32767` | none | `4-46` | Enables selected operation status events. |
| `:STAT:OPER:ENAB?` | none | 16-bit integer | `4-46` | Query operation status enable register. |
| `:STAT:OPER?` or `:STAT:OPER:EVEN?` | none | 16-bit integer | `4-47` | Reads operation event register. Reading clears it. |
| `:STAT:OPER:PRES:COND?` | none | 16-bit integer | `4-48` | Reads instantaneous pressure-operation condition register. |
| `:STAT:OPER:PRES:ENAB <mask>` | 16-bit integer, `0` to `32767` | none | `4-49` | Enables selected pressure-operation events. Manual setup example uses `511`. |
| `:STAT:OPER:PRES:ENAB?` | none | 16-bit integer | `4-49` | Query pressure-operation event enable register. |
| `:STAT:OPER:PRES?` or `:STAT:OPER:PRES:EVEN?` | none | 16-bit integer | `4-50` | Reads pressure-operation event register. Reading clears it. |

Pressure-operation event bits from manual table `3-2`:

| Bit | Meaning |
|---|---|
| 0 | Vent complete |
| 1 | Range change complete |
| 2 | In-limits reached |
| 3 | Zero complete |
| 4 | Auto-zero started |
| 5 | Fill time timed out |
| 8 | Switch contacts changed state |

Manual event setup example for pressure operations:

1. `*CLS`
2. `:STAT:OPER:PRES:ENAB 511`
3. `:STAT:OPER:ENAB 1024`
4. `*SRE 128`
5. Read events with `:STAT:OPER:PRES?` or `:STAT:OPER:PRES:EVEN?`.

For simple polling control, `:SENS:PRES:INL?` may be easier than SRQ/event
handling.

## `:SYSTem` - Errors and System State

| Command / query | Parameters | Response | Ref | Notes |
|---|---|---|---|---|
| `:SYST:ERR?` | none | error code and message, or `0,"No error"` | `3-10`, `4-51` | Error queue holds up to five errors. Query repeatedly until no error remains. |
| `:SYST:DATE <yy>,<mm>,<dd>` | date integers | none | `4-53` | Sets instrument date. Usually not part of experiment control. |
| `:SYST:DATE?` | none | year, month, day | `4-53` | Can be logged if instrument clock matters. |
| `:SYST:SET <mode>,<pressure>` | `MEAS` or `CONT`; setpoint | none | `4-54` | Switch-on setting only. Prefer explicit `:SOUR:PRES` and `:OUTP:STAT` in experiments. |
| `:SYST:SET?` | none | mode and setpoint | `4-54` | Example: `:SYST:SET MEAS, 0.0`. |
| `:SYST:TIME <hh>,<mm>,<ss>` | time integers | none | `4-55` | Sets instrument time. |
| `:SYST:TIME?` | none | hour, minute, second | `4-55` | Can be logged if instrument clock matters. |
| `:SYST:COMM:SER:CONT <integer>` | `0` none, `1` XON/XOFF, `2` RTS/CTS | none | `4-56` | Serial handshaking. Changing communication settings can break the session. |
| `:SYST:COMM:SER:CONT?` | none | `0`, `1`, or `2` | `4-56` | Query serial handshaking. |
| `:SYST:COMM:SER:BAUD <baud>` | valid baud rate | none | `4-57` | Changing baud rate loses communication until the PC matches it. |
| `:SYST:COMM:SER:BAUD?` | none | baud rate | `4-57` | Query serial baud rate. |
| `:SYST:COMM:SER:TYPE:PAR <mode>` | `NONE`, `ODD`, or `EVEN` | none | `4-58` | Changing parity breaks communication until the PC matches it. |
| `:SYST:COMM:SER:TYPE:PAR?` | none | `NONE`, `ODD`, or `EVEN` | `4-58` | Query serial parity. |

Useful `:SYST:ERR?` codes:

| Code | Meaning |
|---|---|
| `-102` | Syntax error |
| `-113` | Undefined header |
| `-114` | Header suffix out of range |
| `-200` | Execution error |
| `-222` | Data out of range |
| `-350` | Queue overflow |
| `-400` | Query error |
| `201` | Query only |
| `202` | No query allowed |
| `211` | Unit not matched |

## Local/Remote Control

| Command | Parameters | Response | Ref | Notes |
|---|---|---|---|---|
| `:GTL` | none | none | `4-74` | Go to local; takes the instrument out of local lockout mode. |
| `:LOC` | none | none | `4-75` | Puts the instrument into local mode and enables front-panel operation. |

Most SCPI commands put the PACE into remote control mode and disable the front
panel touch screen. Use `:LOC` when returning control to an operator.

## Commands to Exclude from Routine DAC Scripts

These commands are included here because they are important to recognise, but
they should not be mixed into normal automated membrane pressure stepping.

| Command family | Why to avoid in routine scripts | Ref |
|---|---|---|
| `:SOUR:PRES:LEV:IMM:AMPL:VENT 1` | Vents the user system. This is a major pressure-state change and should be an explicit operator action. | `4-39` |
| `:CAL:ZERO:VALV...` | Manual warning: opening the zero valve with high pressure in the system can damage equipment. Reduce pressure and make sure the controller is off first. | `4-6`, `4-7` |
| `:CAL:ZERO:AUTO 1` | Starts a zero process. Query returns `0` zero OK, `1` in progress. Use only in maintenance/calibration workflows. | `4-8` |
| `:CAL:PRES...` | Calibration-only commands, valid only when calibration is enabled. | `4-3` to `4-5` |
| `:UNIT:PRES:DEF...` | Redefines user pressure units, making logs ambiguous unless carefully controlled. | `4-66` |
| `:SYST:COMM:SER:...` | Can break serial communication until the PC setting is changed to match. | `4-56` to `4-58` |
| `:SYST:SET CONT,<pressure>` | Changes switch-on behaviour, not the normal immediate pressure-control sequence. | `4-54` |

## Implementation Parsing Hints

For future code work, separate query handling into two styles:

| Style | Use for |
|---|---|
| Single-value parsing | `:SENS:PRES?`, `:SOUR:PRES?`, `:SOUR:PRES:SLEW?`, `:OUTP:STAT?`, `:UNIT:PRES?`. |
| Raw-line parsing | `*IDN?`, `:INST:CAT?`, `:INST:LIM?`, `:SENS:PRES:INL?`, `:SYST:ERR?`, any quoted-string or comma-separated response. |

Pressure-changing code should preserve this ordering:

1. Select/query unit.
2. Check +ve source pressure.
3. Set linear slew mode.
4. Set and verify slew rate.
5. Configure overshoot policy if needed.
6. Send setpoint.
7. Enable control only when the desired target/rate state is confirmed.
