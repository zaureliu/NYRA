from __future__ import annotations

import re

from app.tools.shell_models import RiskAssessment, ShellRiskLevel


_RANK = {
    ShellRiskLevel.READ_ONLY: 0,
    ShellRiskLevel.LOW_RISK: 1,
    ShellRiskLevel.ELEVATED: 2,
    ShellRiskLevel.DESTRUCTIVE: 3,
    ShellRiskLevel.CRITICAL: 4,
}


class ShellRiskClassifier:
    """Conservative PowerShell/CMD heuristics without pretending to be a full parser.

    Every chained statement and pipeline component contributes risk. Unknown or
    dynamically generated commands are sensitive by default instead of being
    considered safe merely because they miss a blacklist entry.
    """

    _critical = (
        (re.compile(r"(?i)^\s*(?:format(?:\.com)?|diskpart)(?:\s|$)"), "disk formatting/partition tool"),
        (re.compile(r"(?i)\b(?:format-volume|clear-disk|initialize-disk|remove-partition|resize-partition)\b"), "storage mutation cmdlet"),
        (re.compile(r"(?i)\b(?:bcdedit|bootrec|reagentc)\b"), "boot configuration change"),
        (re.compile(r"(?i)\bset-mppreference\b[^\r\n;|]*(?:disable|exclusion)"), "security control modification"),
        (re.compile(r"(?i)\b(?:disable-windowsdefender|uninstall-windowsdefender)\b"), "security control disablement"),
        (re.compile(r"(?i)(?:remove-item|del|erase|rmdir|rd)\b[^\r\n;|]*(?:[a-z]:\\(?:\s|[\"']?$)|[a-z]:\\\*|\\\\\.\\physicaldrive)"), "mass/root filesystem deletion"),
        (re.compile(r"(?i)(?:^|[;&|]\s*)(?:mkfs(?:\.\w+)?|wipefs|fdisk|cfdisk|parted)(?:\s|$)"), "remote disk formatting/partition tool"),
        (re.compile(r"(?i)\bdd\b[^\r\n;|]*\bof\s*=\s*/dev/"), "raw block device write"),
        (re.compile(r"(?i)\bzpool\s+destroy\b"), "storage pool destruction"),
    )
    _destructive = (
        (re.compile(r"(?i)\b(?:remove-item|clear-content)\b"), "filesystem deletion/content clearing"),
        (re.compile(r"(?i)(?:^|[;&|]\s*)(?:rm|ri|del|erase|rmdir|rd)(?:\s|$)"), "destructive shell alias"),
        (re.compile(r"(?i)(?:^|\s)(?:(?:sudo|doas)\s+)?(?:rm|shred)(?:\s|$)"), "remote filesystem deletion"),
        (re.compile(r"(?i)\[(?:system\.)?io\.(?:file|directory)\]::(?:delete|move)\s*\("), "direct .NET filesystem mutation"),
        (re.compile(r"(?i)\.delete\s*\("), "object deletion method"),
        (re.compile(r"(?i)\bgit\s+reset\b[^\r\n;|]*--hard\b"), "git hard reset"),
        (re.compile(r"(?i)\bgit\s+clean\b[^\r\n;|]*(?:-[a-z]*f|--force)"), "git forced clean"),
        (re.compile(r"(?i)\bdocker\s+(?:system|image|container|volume|builder)\s+prune\b"), "docker prune"),
        (re.compile(r"(?i)\b(?:drop\s+(?:database|schema|table)|truncate\s+table)\b"), "database data removal"),
        (re.compile(r"(?i)\b(?:uninstall-package|winget\s+uninstall|choco\s+uninstall|scoop\s+uninstall)\b"), "software removal"),
        (re.compile(r"(?i)(?:^|[;&|]\s*)(?:apt(?:-get)?\s+(?:remove|purge|autoremove)|dnf\s+remove|yum\s+remove|apk\s+del)\b"), "remote package removal"),
        (re.compile(r"(?i)\b(?:qm|pct)\s+destroy\b"), "virtual machine/container destruction"),
        (re.compile(r"(?i)\bzfs\s+destroy\b"), "filesystem/dataset destruction"),
        (re.compile(r"(?i)\bfind\b[^\r\n;|]*\s-delete\b"), "recursive find deletion"),
    )
    _elevated = (
        (re.compile(r"(?i)\b(?:stop-service|restart-service|suspend-service|set-service)\b"), "service state change"),
        (re.compile(r"(?i)\b(?:set-net\w*|new-netfirewallrule|set-netfirewallrule|remove-netfirewallrule|disable-netadapter|enable-netadapter)\b"), "network/firewall mutation"),
        (re.compile(r"(?i)\bnetsh\b(?![^\r\n;|]*(?:show|dump)\b)"), "netsh state change"),
        (re.compile(r"(?i)\b(?:reg(?:\.exe)?\s+(?:add|delete|import|restore)|set-itemproperty|new-itemproperty|remove-itemproperty)\b"), "registry mutation"),
        (re.compile(r"(?i)\b(?:stop-process|taskkill|shutdown|restart-computer|stop-computer)\b"), "process or system stop"),
        (re.compile(r"(?i)\bstart-process\b[^\r\n;|]*-verb\s+runas\b"), "explicit UAC elevation request"),
        (re.compile(r"(?i)\b(?:takeown|icacls|set-acl|chmod|chown)\b"), "permission/ownership change"),
        (re.compile(r"(?i)\b(?:install-package|winget\s+install|choco\s+install|scoop\s+install|pip\s+install|npm\s+install)\b"), "package installation"),
        (re.compile(r"(?i)\bdocker(?:\.exe)?\s+(?:compose\s+)?(?:down|stop|restart|rm|kill|run|exec|up)\b"), "container state change"),
        (re.compile(r"(?i)\b(?:invoke-expression|iex)\b|(?:powershell|pwsh)(?:\.exe)?\s+[^\r\n]*(?:-enc|-encodedcommand)\b"), "dynamic or encoded execution"),
        (re.compile(r"(?i)(?:^|[;&|]\s*)(?:cmd(?:\.exe)?\s+/c|powershell(?:\.exe)?\s+-command|pwsh(?:\.exe)?\s+-command)\b"), "nested shell execution"),
        (re.compile(r"(?i)\b(?:sc(?:\.exe)?\s+(?:config|stop|delete|create)|schtasks\s+/(?:create|delete|change|run))\b"), "service/task scheduler mutation"),
        (re.compile(r"(?i)\bsystemctl\s+(?:start|stop|restart|try-restart|reload|enable|disable|mask|unmask)\b"), "remote service state change"),
        (re.compile(r"(?i)\bservice\s+[A-Za-z0-9_.@-]+\s+(?:start|stop|restart|reload)\b"), "remote service state change"),
        (re.compile(r"(?i)/etc/init\.d/[A-Za-z0-9_.@-]+\s+(?:start|stop|restart|reload)\b"), "OpenWrt/service state change"),
        (re.compile(r"(?i)(?:^|[;&|]\s*)wifi\s+(?:reload|up|down)\b"), "wireless state change"),
        (re.compile(r"(?i)(?:^|[;&|]\s*)uci\s+(?:set|add|delete|rename|reorder|commit|revert|import)\b"), "OpenWrt configuration change"),
        (re.compile(r"(?i)(?:^|[;&|]\s*)ip\s+(?:addr|address|route|link|neigh)\s+(?:add|del|delete|replace|change|set|flush)\b"), "remote network state change"),
        (re.compile(r"(?i)\b(?:qm|pct)\s+(?:start|stop|shutdown|reboot|reset|suspend|resume|set|create|clone|migrate)\b"), "virtual machine/container state change"),
        (re.compile(r"(?i)(?:^|[;&|]\s*)(?:apt(?:-get)?\s+(?:install|upgrade|dist-upgrade)|dnf\s+(?:install|upgrade)|yum\s+(?:install|update)|apk\s+add)\b"), "remote package/system change"),
    )
    _low = (
        (re.compile(r"(?i)\b(?:new-item|mkdir|md|copy-item|move-item|rename-item|set-content|add-content|out-file|tee-object|start-process)\b"), "reversible local change"),
        (re.compile(r"(?i)(?:^|[;&|]\s*)(?:mkdir|md|copy|move|ren|rename|start)(?:\s|$)"), "reversible shell operation"),
        (re.compile(r"(?i)\bgit\s+(?:switch\s+-c|checkout\s+-b|branch\s+[^-])"), "git branch creation"),
        (re.compile(r"(?i)\b(?:pytest|cargo\s+(?:test|check|build)|npm(?:\.cmd)?\s+(?:test|run\s+(?:test|build|lint)))\b"), "local test/build command"),
        (re.compile(r"(?<![<>=])>{1,2}(?![>=])"), "output redirection writes data"),
    )

    _read_native = {
        "arp", "cmdkey", "docker", "driverquery", "find", "findstr", "git", "hostname",
        "ipconfig", "netstat", "nslookup", "pathping", "ping", "route", "systeminfo", "tasklist",
        "tracert", "type", "ver", "where", "whoami",
    }
    _read_cmdlets = (
        "compare-", "convertfrom-", "convertto-", "format-", "get-", "group-", "measure-",
        "resolve-", "select-", "sort-", "test-", "where-", "out-string",
    )

    def classify(self, command: str, shell: str = "powershell") -> RiskAssessment:
        value = command.strip()
        if not value:
            return RiskAssessment(level=ShellRiskLevel.ELEVATED, reasons=["empty command is invalid"])

        level = ShellRiskLevel.READ_ONLY
        reasons: list[str] = []
        dynamic_reason = self._dynamic_execution_reason(value, shell)
        if dynamic_reason:
            level = ShellRiskLevel.ELEVATED
            reasons.append(dynamic_reason)
        for target_level, rules in (
            (ShellRiskLevel.CRITICAL, self._critical),
            (ShellRiskLevel.DESTRUCTIVE, self._destructive),
            (ShellRiskLevel.ELEVATED, self._elevated),
            (ShellRiskLevel.LOW_RISK, self._low),
        ):
            for pattern, reason in rules:
                if pattern.search(value):
                    level = self._max(level, target_level)
                    reasons.append(reason)

        components = self._components(value)
        for component in components:
            component_level, reason = self._classify_component(component, shell)
            level = self._max(level, component_level)
            if reason:
                reasons.append(reason)

        if self._targets_sensitive_path(value) and level == ShellRiskLevel.LOW_RISK:
            level = ShellRiskLevel.ELEVATED
            reasons.append("write targets a system-wide path")
        if not reasons and level == ShellRiskLevel.READ_ONLY:
            reasons.append("recognized read-only inspection")
        return RiskAssessment(
            level=level,
            reasons=list(dict.fromkeys(reasons)),
            components=components,
        )

    @staticmethod
    def _dynamic_execution_reason(command: str, shell: str) -> str | None:
        """Fail closed when evaluation can be hidden inside arguments."""
        outside_single: list[str] = []
        single = False
        double = False
        escaped = False
        for char in command:
            if escaped:
                if not single:
                    outside_single.append(char)
                escaped = False
                continue
            if char == "`" and not single:
                outside_single.append(char)
                escaped = True
                continue
            if char == "'" and not double:
                single = not single
                continue
            if char == '"' and not single:
                double = not double
                outside_single.append(char)
                continue
            if not single:
                outside_single.append(char)
        visible = "".join(outside_single)

        if shell in {"bash", "ssh", "linux", "openwrt"}:
            if re.search(r"\$\(|`|(?<![<>])[<>]\(", visible):
                return "shell substitution/process substitution requires approval"
            return None

        # PowerShell expressions can invoke members whose names are quoted or
        # computed (for example `.'Delete'()`). Treat every expression/type/
        # index delimiter as dynamic instead of trying to enumerate its AST in
        # regexes. Plain read-only cmdlets with ordinary arguments stay safe.
        if any(char in visible for char in "()[]{}"):
            return "PowerShell expression or type/member evaluation requires approval"
        if re.search(r"\$\(|@\(|`|(?<!&)&(?!&)|[{}]", visible):
            return "PowerShell substitution, call operator or script block requires approval"
        if re.search(r"(?i)(?:::|\.)[A-Za-z_]\w*\s*\(", visible):
            return "PowerShell member invocation requires approval"
        if re.search(r"\(\s*(?:&|\$|[A-Za-z_][\w.-]*(?:\s|\)))", visible):
            return "nested PowerShell expression requires approval"
        return None

    def _classify_component(self, component: str, shell: str) -> tuple[ShellRiskLevel, str]:
        text = component.strip().lstrip("(&{").strip()
        if not text or text.startswith("#") or text.startswith("::"):
            return ShellRiskLevel.READ_ONLY, ""
        match = re.match(r'''(?i)(?:&\s*)?["']?([^\s"']+)''', text)
        if not match:
            return ShellRiskLevel.ELEVATED, "unparsed command component"
        executable = match.group(1).casefold()
        executable = executable.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        executable = executable[:-4] if executable.endswith(".exe") else executable

        if executable in self._read_native:
            return self._native_risk(executable, text)
        if executable.startswith(self._read_cmdlets):
            return ShellRiskLevel.READ_ONLY, ""
        if executable in {"start-sleep", "write-output", "write-error", "write-warning", "write-verbose", "write-host"}:
            return ShellRiskLevel.READ_ONLY, ""
        if shell in {"bash", "ssh", "linux", "openwrt"}:
            remote = self._remote_risk(executable, text)
            if remote is not None:
                return remote
        if executable in {"cd", "chdir", "dir", "echo", "exit", "pwd", "ls", "gc", "cat"}:
            return ShellRiskLevel.READ_ONLY, ""
        if executable in {
            "new-item", "mkdir", "md", "copy-item", "copy", "move-item", "move",
            "rename-item", "ren", "rename", "set-content", "add-content", "out-file",
            "tee-object", "start-process", "start",
        }:
            return ShellRiskLevel.LOW_RISK, "reversible local change"
        if executable in {"pytest", "cargo", "npm", "npm.cmd"}:
            return ShellRiskLevel.LOW_RISK, "local test/build command"
        if executable in {"foreach-object", "%", "for", "foreach", "if", "try"}:
            return ShellRiskLevel.ELEVATED, "script block/control flow requires review"
        if executable.startswith("$") or executable in {"call", "invoke-command", "start-job", "&"}:
            return ShellRiskLevel.ELEVATED, "indirect command execution"
        return ShellRiskLevel.ELEVATED, f"unrecognized executable/cmdlet: {executable[:80]}"

    @staticmethod
    def _remote_risk(executable: str, text: str) -> tuple[ShellRiskLevel, str] | None:
        lowered = text.casefold()
        simple_read = {
            "uptime", "uname", "hostname", "df", "free", "ps", "who", "w", "id", "date",
            "ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "env", "printenv",
            "dmesg", "logread", "pveversion", "iw", "iwinfo", "ifstatus", "top", "vmstat",
            "lsblk", "findmnt",
        }
        if executable in simple_read:
            return ShellRiskLevel.READ_ONLY, ""
        if executable == "ip":
            if re.search(r"\b(?:add|del|delete|replace|change|set|flush)\b", lowered):
                return ShellRiskLevel.ELEVATED, "remote network state change"
            return ShellRiskLevel.READ_ONLY, ""
        if executable == "systemctl":
            if "--failed" in lowered or re.search(r"\b(?:status|is-active|is-failed|show|list-units|list-unit-files)\b", lowered):
                return ShellRiskLevel.READ_ONLY, ""
            return ShellRiskLevel.ELEVATED, "remote service operation"
        if executable == "service":
            return (ShellRiskLevel.READ_ONLY, "") if re.search(r"\b(?:status|--status-all)\b", lowered) else (ShellRiskLevel.ELEVATED, "remote service operation")
        if re.search(r"(?i)/etc/init\.d/[A-Za-z0-9_.@-]+\s+status\b", text):
            return ShellRiskLevel.READ_ONLY, ""
        if executable in {"journalctl", "pveversion"}:
            return ShellRiskLevel.READ_ONLY, ""
        if executable in {"docker", "podman"}:
            if re.search(r"\b(?:ps|images|inspect|logs|stats|info|version|network\s+ls|volume\s+ls|compose\s+(?:ps|logs|config|ls))\b", lowered):
                return ShellRiskLevel.READ_ONLY, ""
            return ShellRiskLevel.ELEVATED, "remote container state change"
        if executable == "pvesh":
            return (ShellRiskLevel.READ_ONLY, "") if re.search(r"\bpvesh\s+get\b", lowered) else (ShellRiskLevel.ELEVATED, "Proxmox API mutation")
        if executable in {"qm", "pct"}:
            return (ShellRiskLevel.READ_ONLY, "") if re.search(r"\b(?:list|status|config|pending)\b", lowered) else (ShellRiskLevel.ELEVATED, "virtual machine/container state change")
        if executable == "pvesm":
            return (ShellRiskLevel.READ_ONLY, "") if re.search(r"\b(?:status|list|path)\b", lowered) else (ShellRiskLevel.ELEVATED, "storage state change")
        if executable in {"zfs", "zpool"}:
            return (ShellRiskLevel.READ_ONLY, "") if re.search(r"\b(?:list|status|get)\b", lowered) else (ShellRiskLevel.ELEVATED, "storage state change")
        if executable == "ubus":
            return (ShellRiskLevel.READ_ONLY, "") if re.search(r"\b(?:list|call)\b", lowered) else (ShellRiskLevel.ELEVATED, "OpenWrt ubus mutation")
        if executable == "uci":
            return (ShellRiskLevel.READ_ONLY, "") if re.search(r"\b(?:show|get|changes|export)\b", lowered) else (ShellRiskLevel.ELEVATED, "OpenWrt configuration change")
        if executable == "wifi":
            return (ShellRiskLevel.READ_ONLY, "") if re.search(r"\bstatus\b", lowered) else (ShellRiskLevel.ELEVATED, "wireless state change")
        if executable in {"sudo", "su", "sh", "bash", "ash"}:
            return ShellRiskLevel.ELEVATED, "indirect or privileged remote execution"
        return None

    @staticmethod
    def _native_risk(executable: str, text: str) -> tuple[ShellRiskLevel, str]:
        lowered = text.casefold()
        if executable == "git":
            if re.search(r"\bgit\s+(?:status|log|diff|show|branch\s+--show-current|rev-parse|ls-files|remote\s+-v)\b", lowered):
                return ShellRiskLevel.READ_ONLY, ""
            if re.search(r"\bgit\s+(?:switch\s+-c|checkout\s+-b|branch\s+[^-])", lowered):
                return ShellRiskLevel.LOW_RISK, "git branch creation"
            return ShellRiskLevel.ELEVATED, "git operation may change repository state"
        if executable == "docker":
            if re.search(r"\bdocker\s+(?:ps|images|inspect|logs|stats|info|version|network\s+ls|volume\s+ls|compose\s+(?:ps|logs|config|ls))\b", lowered):
                return ShellRiskLevel.READ_ONLY, ""
            return ShellRiskLevel.ELEVATED, "docker operation may change container state"
        if executable == "route" and not re.search(r"\b(?:print|show)\b", lowered):
            return ShellRiskLevel.ELEVATED, "route operation may change networking"
        if executable == "cmdkey" and not re.search(r"/(?:list|l)(?:\s|$)", lowered):
            return ShellRiskLevel.ELEVATED, "credential store operation may change state"
        return ShellRiskLevel.READ_ONLY, ""

    @staticmethod
    def _components(command: str) -> list[str]:
        components: list[str] = []
        current: list[str] = []
        quote: str | None = None
        escaped = False
        depth = 0
        index = 0
        while index < len(command):
            char = command[index]
            if escaped:
                current.append(char)
                escaped = False
                index += 1
                continue
            if char == "`":
                current.append(char)
                escaped = True
                index += 1
                continue
            if quote:
                current.append(char)
                if char == quote:
                    quote = None
                index += 1
                continue
            if char in {"'", '"'}:
                quote = char
                current.append(char)
                index += 1
                continue
            if char in "{([":
                depth += 1
                current.append(char)
                index += 1
                continue
            if char in "})]":
                depth = max(0, depth - 1)
                current.append(char)
                index += 1
                continue
            two = command[index:index + 2]
            if depth == 0 and (char in {";", "|", "\n", "\r"} or two in {"&&", "||"}):
                value = "".join(current).strip()
                if value:
                    components.append(value)
                current = []
                index += 2 if two in {"&&", "||"} else 1
                continue
            current.append(char)
            index += 1
        value = "".join(current).strip()
        if value:
            components.append(value)
        return components

    @staticmethod
    def _targets_sensitive_path(command: str) -> bool:
        return bool(re.search(
            r"(?i)(?:[a-z]:\\(?:windows|program files(?: \(x86\))?|programdata)(?:\\|\b)|hklm:|hkey_local_machine)",
            command,
        ))

    @staticmethod
    def _max(left: ShellRiskLevel, right: ShellRiskLevel) -> ShellRiskLevel:
        return left if _RANK[left] >= _RANK[right] else right
