# OWON SPE6103 Protocol Reference

This protocol specification documents the **exact commands and response formats** recovered from `winApp.jar` inside [`Owon/DC Power Supply Control.exe`](file:///e:/Project/BMS/Major/Charge%20REA/Owon/DC%20Power%20Supply%20Control.exe). Every entry below distinguishes directly verified bytecode behavior from unverified or unused SCPI conventions.

---

## 1. Physical & Framing Parameters

| Parameter | Value | Evidence Source |
|---|---|---|
| Interface | USB Virtual COM Port (`"MyCommCH340"` CH340 bridge) | `ComAgent` (`open("MyCommCH340", 2000)`) |
| Baud Rate | `115200` bps (`protocol= com`) | `conf/pref.json`, `application.Main`, `Global.setDefaultBaudRate` |
| Data Bits / Stop Bits / Parity | `8` / `1` / `None` | `conf/pref.json`, `ComAgent` |
| Character Encoding | ASCII | `ComAgent` stream read/write |
| Line Terminator | LF (`\n`, `0x0A`) | `ComAgent.sendCommand` |
| Recommended Pacing | `>= 100 ms` between commands | `ComAgent.WriteDelay = 100`, `ReadDelay = 100` |
| Open / Read Timeout | `2000 ms` (`2.0 s`) | `ComAgent.open("MyCommCH340", 2000)` |

---

## 2. Verified Command Summary (`winApp.jar`)

All commands below were observed directly in `ComAgent`, `SpeParamService`, `ChannelSettingController`, `KeyPressListenFxUtil$1`, and `MemoryBean`:

| Category | Operation | Exact Command | Direction / Response | Status |
|---|---|---|---|---|
| **Identity** | Identify instrument | `*IDN?` | `<Manufacturer>,<Model>,<Serial>,<FW>` | Verified (bytecode) |
| **Session** | Enter PC remote lock | `SYSTem:REMote` | Write only (sent after connect sync) | Verified (bytecode) |
| **Session** | Return front panel to local | `SYSTem:LOCal` | Write only (sent before port close) | Verified (bytecode) |
| **Output** | Enable DC output | `OUTP ON` | Write only (ChannelSettingController) | Verified (bytecode) |
| **Output** | Disable DC output | `OUTP OFF` | Write only (ChannelSettingController) | Verified (bytecode) |
| **Output** | Query output state | `OUTP?` | `"ON"` or `"OFF"` (`SpeParamService$1`) | Verified (bytecode) |
| **Setpoint** | Set target voltage | `VOLT <val>` | Write only (e.g., `VOLT 12.000`) | Verified (bytecode) |
| **Setpoint** | Query voltage setpoint | `VOLT?` | ASCII float (e.g., `12.000`) | Verified (bytecode) |
| **Setpoint** | Set CC current limit | `CURR <val>` | Write only (e.g., `CURR 2.000`) | Verified (bytecode) |
| **Setpoint** | Query CC current limit | `CURR?` | ASCII float (e.g., `2.000`) | Verified (bytecode) |
| **Protection** | Set OVP threshold | `VOLT:LIM <val>` | Write only (independent from `VOLT`) | Verified (bytecode) |
| **Protection** | Query OVP threshold | `VOLT:LIM?` | ASCII float | Verified (bytecode) |
| **Protection** | Set OCP threshold | `CURR:LIM <val>` | Write only (independent from `CURR`) | Verified (bytecode) |
| **Protection** | Query OCP threshold | `CURR:LIM?` | ASCII float | Verified (bytecode) |
| **Telemetry** | Poll V/I/P, trips & mode | `MEASure:ALL:INFO?` | 7 comma-separated fields (see Section 3) | Verified (bytecode) |
| **Telemetry** | Legacy FitMa poll | `MEASure:ALL?` | 6 comma-separated fields (`isFitMaProtocol=true`) | Verified (bytecode) |

---

## 3. Telemetry & Protection Status Format (`MEASure:ALL:INFO?`)

`ComAgent$CmdDeamonThread` polls `MEASure:ALL:INFO?` continuously and dispatches the comma-separated payload to `SpeParamService$7`:

```text
<meas_V>,<meas_I>,<meas_P>,<OVP_trip>,<OCP_trip>,<OTP_trip>,<mode_code>\n
```

| Field Index | Meaning | Type / Values | UI Target in `SpeController` |
|---|---|---|---|
| `0` | Measured output voltage | Float string (V) | `measVolt` |
| `1` | Measured output current | Float string (A) | `measCurr` |
| `2` | Measured output power | Float string (W) | `measPw` |
| `3` | OVP trip status | `"ON"` (tripped) / `"OFF"` (ok) | `errorView_voltLimit` |
| `4` | OCP trip status | `"ON"` (tripped) / `"OFF"` (ok) | `errorView_currLimit` |
| `5` | OTP (thermal) trip status | `"ON"` (tripped) / `"OFF"` (ok) | `errorView_tempLimit` |
| `6` | Operating state code | `"0"` Standby, `"1"` CV, `"2"` CC, `"3"` Fault | Mode status display |

When legacy mode `Global.isFitMaProtocol == true` is checked (`"Switch to MA protocol"`), the app sends `MEASure:ALL?` instead and omits field `6` (`mode_code`).

---

## 4. Setpoint vs. Protection Semantics

1. **`VOLT <v>` vs `VOLT:LIM <v>`**:
   - `VOLT` sets the regulated DC output target voltage (`0.000` .. `60.000` V).
   - `VOLT:LIM` configures the independent hardware Over-Voltage Protection trip threshold (`0.000` .. `61.000` V in `Config.SPE6103`).
2. **`CURR <i>` vs `CURR:LIM <i>`**:
   - `CURR` sets the programmable Constant-Current (CC) compliance ceiling (`0.000` .. `10.000` A). Reaching this current puts the supply smoothly into CC mode (`mode_code = "2"`) without tripping a fault.
   - `CURR:LIM` configures the independent Over-Current Protection (OCP) trip threshold (`0.000` .. `10.100` A in `Config.SPE6103`), which faults and shuts off the output if exceeded (`OCP_trip = "ON"`, `mode_code = "3"`).

---

## 5. Commands NOT Used / Unverified in `winApp.jar`

The following standard SCPI commands do **not** appear anywhere in `winApp.jar` and are **not** used by [`python_controller`](file:///e:/Project/BMS/Major/Charge%20REA/python_controller):

- `VOLTage:PROTection` / `CURRent:PROTection` (unverified; real app uses `VOLT:LIM` / `CURR:LIM`)
- `SYSTem:ERRor?` (unverified; real app checks OVP/OCP/OTP/mode inside `MEASure:ALL:INFO?`)
- `*RST`, `*CLS` (unverified; absent from `winApp.jar`)
- `MEASure:VOLTage?`, `MEASure:CURRent?`, `MEASure:POWer?` (unverified as standalone queries; `python_controller` queries `MEASure:ALL:INFO?` directly)

*(Note: `winApp.jar` also bundles `delegate.modbus.ModBusAgent` + `service.WsdParamService` using CRC-16 Modbus RTU when `ini.pro` specifies `protocol= mod`, which is inactive for the SPE6103 `protocol= com` setup.)*
