# Reverse Engineering Report: OWON SPE6103 Control Application

This document records the static and bytecode reverse-engineering findings for the original OWON Windows application located in [`Owon/`](file:///e:/Project/BMS/Major/Charge%20REA/Owon), conducted before finalizing the independent Python controller.

---

## 1. Application Inventory & Packaging

Inspection of `Owon\` found:

| Path | Size (bytes) | Role |
|---|---|---|
| [`Owon/DC Power Supply Control.exe`](file:///e:/Project/BMS/Major/Charge%20REA/Owon/DC%20Power%20Supply%20Control.exe) | 1,520,128 | PE32+ (amd64) `exe4j` wrapper executable (imports `USER32`, `ADVAPI32`, `COMCTL32`, `KERNEL32`; imphash `960cc3b459a0e26f574d39202a746b3b`) |
| [`Owon/ini.pro`](file:///e:/Project/BMS/Major/Charge%20REA/Owon/ini.pro) | 51 | Application protocol selector (`pjTitle = DC Power Supply Control`, `protocol= com`) |
| [`Owon/conf/pref.json`](file:///e:/Project/BMS/Major/Charge%20REA/Owon/conf/pref.json) | 11,623 | Serial defaults (`COM3`, `115200`, `8`, `1`, `None`, `"isFitMaProtocol": false`, `"ComType": 0`) |
| `Owon/jre_x64/lib/RXTXcomm.jar` | 60,984 | RXTX Java serial communications library (`gnu.io.*`) |
| `Owon/jre_x64/bin/rxtxSerial.dll` | 129,536 | Native 64-bit Windows serial port DLL |
| `Owon/jre_x64/bin/msvcp120.dll` | 660,128 | MSVC 2013 runtime dependency for `rxtxSerial.dll` |

No external application `.jar` files exist alongside the `.exe`. Instead, `DC Power Supply Control.exe` embeds its Java payload inside an `exe4j` PE overlay starting at file offset `0x38400` (`1,289,728` bytes, header magic `d5 13 e4 e8`, obfuscated via single-byte `XOR 0x88`). Extracting and un-XORing that overlay recovers two standard ZIP/JAR archives:

1. `exe4jlib.jar` (40 entries) -- `exe4j` launcher runtime.
2. `winApp.jar` (400 entries, `Main-Class: application.Main`, UI label `"V1.6.0"`, `"Fujian Lilliput Optoelectronics Technology Co.,Ltd. 20220617."`).

---

## 2. Architecture & Transport Mechanism

JVM bytecode disassembly of `winApp.jar` reveals a Java 8 + JavaFX desktop application structured into:

- **Entry & Wiring (`application.Main`)**:
  - Reads `ini.pro` property `protocol` and switches on `storeroom.util.SPool` constants:
    - `Protocol_Modbus = "mod"` -> `delegate.modbus.ModBusAgent` (`9600` default baud)
    - `Protocol_Comm = "com"` -> **`delegate.serial.ComAgent`** (`115200` default baud)
    - `Protocol_Net = "net"` -> `delegate.net.NetAgent` (`java.net.Socket` with `4000` ms timeout)
  - Because [`Owon/ini.pro`](file:///e:/Project/BMS/Major/Charge%20REA/Owon/ini.pro) sets `protocol= com`, the SPE6103 control path runs exclusively on `delegate.serial.ComAgent`.
- **Serial Transport (`delegate.serial.ComAgent`, `SerialOpenAndClose`, `SerialPortUtil`)**:
  - Uses RXTX (`gnu.io.RXTXPort` / `rxtxSerial.dll`) over a Windows Virtual COM Port (VCP).
  - Opens the COM port with owner identifier **`"MyCommCH340"`** (`open("MyCommCH340", 2000)` -> 2000 ms timeout), indicating a CH340 USB-to-UART bridge inside the instrument/adapter.
  - Serial framing parameters (from `conf/pref.json`, `Global`, and `ComAgent`): `115200` baud, `8` data bits, `1` stop bit, parity `None` (`0`), ASCII `\n` (LF) line termination.
  - Timing constants directly encoded in `ComAgent`: `WriteDelay = 100` ms, `ReadDelay = 100` ms (`Thread.sleep(100)`), and `JudgeDisConnectCount = 50`.

---

## 3. Connection, Sync, Polling & Teardown Sequence

Disassembly of `delegate.serial.ComAgent` and `service.SpeParamService` (`$1`..`$7`) shows the exact runtime lifecycle:

1. **Port Open & Initial Synchronization (`ComAgent.initRequest()`)**:
   - Sends 6 queries sequentially (never enables DC output on connect):
     1. `OUTP?\n` -> `SpeParamService$1` checks `value.equals("ON")`
     2. `VOLT?\n` -> `SpeParamService$2` updates voltage setpoint input field
     3. `CURR?\n` -> `SpeParamService$3` updates programmable current limit field
     4. `VOLT:LIM?\n` -> `SpeParamService$4` updates OVP threshold field
     5. `CURR:LIM?\n` -> `SpeParamService$5` updates OCP threshold field
     6. `*IDN?\n` -> `SpeParamService$6` splits response on `,` (`[1]` initializes `speConfig.Config.initStaticConfig` + `machineType`, `[2]` serial number `osType`, `[3]` firmware `hardwareType`).
2. **Remote Mode Transition (`ComAgent.accumulateSyncCount()`)**:
   - After the 5 setpoint/state sync callbacks succeed (`syncCount >= 5`), `ComAgent` sets `connectStatus = 3` (`CONNECTED_MEAS`) and writes **`SYSTem:REMote\n`**.
3. **Continuous Telemetry Daemon (`ComAgent$CmdDeamonThread.run()`)**:
   - While `connectStatus == 3`, polls **`MEASure:ALL:INFO?\n`** (or **`MEASure:ALL?\n`** when `Global.isFitMaProtocol == true`) and services queued writes (`write()` inserts at head via `addFirst`) and reads (`readRequest()` appends via `add`).
   - `SpeParamService$7` parses `MEASure:ALL:INFO?` by splitting on `,`:
     - `[0]` Measured voltage (V)
     - `[1]` Measured current (A)
     - `[2]` Measured power (W)
     - `[3]` OVP protection trip indicator (`"ON"` drives red LED `errorView_voltLimit`)
     - `[4]` OCP protection trip indicator (`"ON"` drives red LED `errorView_currLimit`)
     - `[5]` OTP (over-temperature protection) trip indicator (`"ON"` drives red LED `errorView_tempLimit`)
     - `[6]` Operating state digit: `"0"` = Standby, `"1"` = CV, `"2"` = CC, `"3"` = Fault (omitted/ignored when `isFitMaProtocol == true`)
4. **Disconnection (`ComAgent.release()`)**:
   - Writes **`SYSTem:LOCal\n`**, waits up to `200` ms for the daemon thread to exit (`join(200)`), and closes the I/O streams and serial port.

---

## 4. Setpoints vs. Independent Protection Commands

Disassembly of `KeyPressListenFxUtil$1`, `MemoryBean`, `ChannelSettingController`, and `SpeController$3` confirms strict separation between output setpoints and protection thresholds:

| Operation | Verified Wire Command | Bytecode Evidence |
|---|---|---|
| Voltage setpoint | `VOLT <value>\n` / `VOLT?\n` | `KeyPressListenFxUtil$1`, `MemoryBean`, `SpeParamService$2` |
| Programmable current limit (CC) | `CURR <value>\n` / `CURR?\n` | `KeyPressListenFxUtil$1`, `MemoryBean`, `SpeParamService$3` |
| Independent OVP threshold | `VOLT:LIM <value>\n` / `VOLT:LIM?\n` | `KeyPressListenFxUtil$1`, `MemoryBean`, `SpeParamService$4` |
| Independent OCP threshold | `CURR:LIM <value>\n` / `CURR:LIM?\n` | `KeyPressListenFxUtil$1`, `MemoryBean`, `SpeParamService$5` |
| DC Output Enable/Disable | `OUTP ON\n` / `OUTP OFF\n` / `OUTP?\n` | `ChannelSettingController`, `SpeParamService$1` |

In `speConfig.Config`, the `SPE6103` entry defines `MAXV = 61.0`, `MAXA = 10.1`, `MAXP = 312.0` so UI fields allow setting OVP (`VOLT:LIM`) up to `61.0 V` and OCP (`CURR:LIM`) up to `10.1 A` above the rated `60.0 V` / `10.0 A` output ratings. `ComAgent.limitCurrent(V, I)` clamps current to `MAXP / V` when `V * I > MAXP`.

---

## 5. Inactive Secondary Modes & Exhaustive String Check

An exhaustive scan of UTF-8 constant pools across all 400 `.class` files in `winApp.jar` confirmed:

- **Dormant Modbus / Network Modules**: `delegate.modbus.ModBusAgent` + `service.WsdParamService` + `storeroom.util.Crc16Util` implement Modbus RTU (`9600` baud, polynomial `0xA001`) for `protocol= mod` devices (`OWP*H` series), and `delegate.net.NetAgent` handles `protocol= net`. Neither is active when `ini.pro` specifies `protocol= com`.
- **SCPI Commands NOT Present in `winApp.jar`**: `SYSTem:ERRor?`, `*RST`, `*CLS`, `VOLTage:PROTection`, `CURRent:PROTection`, `MEASure:VOLTage?`, `MEASure:CURRent?`, and `MEASure:POWer?` never appear anywhere in `winApp.jar`. Errors/faults in the SPE6103 application are read exclusively via the `OVP`/`OCP`/`OTP`/mode fields of `MEASure:ALL:INFO?`.

---

## 6. Evidence Confidence & Limitations

- **Verified by Static & Bytecode Disassembly**: Executable overlay extraction, `ini.pro` / `pref.json` parsing, RXTX `"MyCommCH340"` serial configuration, all 16 ASCII/SCPI command strings listed in [`protocol_reference.md`](file:///e:/Project/BMS/Major/Charge%20REA/docs/protocol_reference.md), `MEASure:ALL:INFO?` field ordering, and `100` ms `ComAgent` write/read pacing.
- **Pending Live Hardware Capture**: No USB-serial COM ports were attached to the analysis machine (`[System.IO.Ports.SerialPort]::GetPortNames()` and `python main.py --list-ports` returned none), so live USBPcap/Wireshark captures and physical instrument verification remain to be run against actual SPE6103 hardware.
