/* MeetingRecorder.app 主程序启动器（Mach-O 二进制）
 *
 * 为什么不用脚本：macOS TCC（麦克风授权）只为 Mach-O 二进制建立代码身份，
 * 脚本型主程序会导致系统既不弹授权框、也不在授权列表记录条目。
 * 本启动器 chdir 到项目目录后 exec .venv 的 python（强制 arm64 原生架构）。
 */
#include <stdlib.h>
#include <unistd.h>
#include <stdio.h>

int main(void) {
    const char *dir = "/Users/eric/WorkBuddy/2026-10-16-16-03-31/meeting-recorder";
    if (chdir(dir) != 0) {
        perror("chdir failed");
        return 1;
    }
    /* hf 镜像（模型下载走国内网络） */
    setenv("HF_ENDPOINT", "https://hf-mirror.com", 0);
    execl("/usr/bin/arch", "arch", "-arm64",
          "/Users/eric/WorkBuddy/2026-10-16-16-03-31/meeting-recorder/.venv/bin/python",
          "/Users/eric/WorkBuddy/2026-10-16-16-03-31/meeting-recorder/run_recorder.py",
          (char *)NULL);
    perror("exec failed");
    return 1;
}
