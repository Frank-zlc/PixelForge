# vendor/

二进制依赖放这里，**锁版本**，不要依赖 `$PATH`。这些文件不进 git。

| 文件 | 用途 | 缺失后果 |
|------|------|---------|
| `platform-tools/adb` | adb 客户端 | 设备功能全部禁用 |
| `scrcpy-server.jar` | 投屏 + 低延迟控制 | 无实时画面、无低延迟控制；截图与控件树仍可用 |
| `uiautomator2-server.apk` + `uiautomator2-server-test.apk` | 常驻控件树服务 | 控件选择器不可用；模板与 OCR 仍可用 |

## 为什么 adb 要 vendor

默认 `127.0.0.1:5037` 是机器级单例。同事打开一次 Android Studio，或任何人执行
`adb kill-server`，你所有设备会话瞬间全断。而且两个不同版本的 adb 会互相 kill 对方的
server。PixelForge 默认用独立端口 5038（`PIXELFORGE_ADB_SERVER_PORT`），但二进制版本
仍需锁定。

## 版本必须匹配

`scrcpy-server.jar` 的版本要与 `ScrcpyConfig.scrcpy_version` 一致（当前默认 `3.1`）。
服务端会校验并在不匹配时退出——这是最友好的失败方式，另一种是解码出一堆花屏。

```bash
# 示例：取 scrcpy 3.1 的 server jar
curl -L -o scrcpy-server.jar \
  https://github.com/Genymobile/scrcpy/releases/download/v3.1/scrcpy-server-v3.1
```

## uiautomator2 server

两个 APK 都要装，并用 `am instrument` 拉起（PixelForge 会自动做）：

```bash
adb install -r uiautomator2-server.apk
adb install -r uiautomator2-server-test.apk
```

不装也能用——控件树会退化到 `uiautomator dump`（慢 1–3 秒，且页面有动画时直接失败）。
游戏这类没有无障碍信息的界面本来就用不上它。
