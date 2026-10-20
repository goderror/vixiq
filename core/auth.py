# core/auth.py
"""
获取 Pixiv cookies。从旧 get_cookies.py 迁移,改造点:
- 旧脚本用 input("按回车保存")阻塞,GUI 模式不可行
- 拆成 open_login_page + save_cookies 两步,由 GUI CookiesDialog 控制:
  1. GUI 用 make_browser 开浏览器
  2. open_login_page 导航到 pixiv
  3. 用户手动登录后,GUI 点【保存】触发 save_cookies
- 安全层:save_cookies 使用 Windows DPAPI 加密存储(utils.sec.save_encrypted_cookies),
  写盘文件内容为乱码(base64+DPAPI 密文),仅绑定本机+当前 Windows 用户可解密。
"""
from __future__ import annotations

from pathlib import Path

from utils.reporter import Reporter, PrintReporter
from utils.sec import save_encrypted_cookies, secure_delete


def open_login_page(driver, reporter: "Reporter | None" = None) -> None:
    """导航到 pixiv 首页,等用户手动登录。"""
    driver.get("https://www.pixiv.net/")
    if reporter:
        reporter.log("已打开 pixiv,请在浏览器中登录后回到此处点【保存 Cookies】")


def save_cookies(driver, cookies_path: "str | Path", reporter: "Reporter | None" = None) -> bool:
    """把当前 driver 的 cookies 用 Windows DPAPI 加密保存到 cookies_path(.bin)。

    额外动作:
    - 原子写入(先 .tmp 再 replace),避免中途掉电写坏
    - 写盘后调用 icacls 锁 ACL:仅当前 Windows 用户完全控制,移除他人读权限
    - 检测同级目录是否存在旧版明文 cookies.txt,存在则安全删除(覆盖3次再删)
    """
    cookies_path = Path(cookies_path)
    cookies_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cookies = driver.get_cookies()
        real_path = save_encrypted_cookies(cookies, cookies_path, lock_acl=True)

        # 升级/清理: 删掉旧版明文 cookies.txt(在同一 data 目录下找)
        legacy_txt = real_path.with_suffix(".txt")
        if legacy_txt.is_file():
            try:
                secure_delete(legacy_txt)
                if reporter:
                    reporter.log("检测到旧版明文 cookies.txt,已安全删除(升级为加密 cookies.bin)")
            except Exception:
                pass  # 删除失败不影响本次保存

        if reporter:
            reporter.log(f"Cookies 已加密保存到 {real_path}(绑定本机,仅当前 Windows 用户可解密)")
        return True
    except Exception as e:
        if reporter:
            reporter.error(f"保存 cookies 失败: {e}")
        return False
