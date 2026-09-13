"""既有适配器能力声明；兼容值原样迁移，不代表现场验收。"""
from asterun.contracts import Capability, SupportLevel, VerificationStatus


def builtin_capability_rows(kind: str) -> list[Capability]:
    if kind == "fake":
        verified = VerificationStatus.OFFLINE_VERIFIED
        env = "offline-fake"
        return [
            Capability("discover", SupportLevel.SUPPORTED, SupportLevel.SUPPORTED, verified, env),
            Capability("create_session", SupportLevel.SUPPORTED, SupportLevel.SUPPORTED, verified, env),
            Capability("resume_session", SupportLevel.UNSUPPORTED, SupportLevel.UNSUPPORTED, VerificationStatus.NOT_TESTED),
            Capability("submit", SupportLevel.SUPPORTED, SupportLevel.SUPPORTED, verified, env),
            Capability("snapshot_review", SupportLevel.SUPPORTED, SupportLevel.SUPPORTED, verified, env),
            Capability("observe", SupportLevel.SUPPORTED, SupportLevel.SUPPORTED, verified, env),
            Capability("interrupt", SupportLevel.SUPPORTED, SupportLevel.SUPPORTED, verified, env),
            Capability("approval", SupportLevel.SUPPORTED, SupportLevel.SUPPORTED, verified, env),
            Capability("native_reference", SupportLevel.UNSUPPORTED, SupportLevel.UNSUPPORTED, VerificationStatus.NOT_TESTED),
            Capability("usage", SupportLevel.UNSUPPORTED, SupportLevel.UNSUPPORTED, VerificationStatus.NOT_TESTED),
        ]
    if kind == "codex":
        declared = SupportLevel.SUPPORTED
        adapter = SupportLevel.SUPPORTED
        unverified = VerificationStatus.NOT_TESTED
        return [
            Capability("discover", declared, adapter, unverified),
            Capability("create_session", declared, adapter, unverified),
            Capability("resume_session", declared, adapter, unverified),
            Capability("submit", declared, adapter, unverified),
            Capability("snapshot_review", SupportLevel.UNKNOWN, SupportLevel.UNSUPPORTED, unverified),
            Capability("observe", declared, adapter, unverified),
            Capability("interrupt", declared, adapter, unverified),
            Capability("approval", declared, adapter, unverified),
            Capability("native_reference", declared, adapter, unverified),
            Capability("usage", SupportLevel.UNKNOWN, SupportLevel.UNKNOWN, unverified),
        ]
    if kind == "grok":
        unverified = VerificationStatus.NOT_TESTED
        return [
            Capability("discover", SupportLevel.SUPPORTED, SupportLevel.SUPPORTED, unverified),
            Capability("create_session", SupportLevel.SUPPORTED, SupportLevel.SUPPORTED, unverified),
            Capability(
                "resume_session",
                SupportLevel.UNKNOWN,
                SupportLevel.UNSUPPORTED,
                unverified,
            ),
            Capability("submit", SupportLevel.SUPPORTED, SupportLevel.SUPPORTED, unverified),
            Capability("snapshot_review", SupportLevel.UNKNOWN, SupportLevel.UNSUPPORTED, unverified),
            Capability("observe", SupportLevel.SUPPORTED, SupportLevel.SUPPORTED, unverified),
            Capability("interrupt", SupportLevel.UNKNOWN, SupportLevel.UNKNOWN, unverified),
            Capability("approval", SupportLevel.UNKNOWN, SupportLevel.UNKNOWN, unverified),
            Capability("native_reference", SupportLevel.SUPPORTED, SupportLevel.SUPPORTED, unverified),
            Capability("usage", SupportLevel.UNKNOWN, SupportLevel.UNKNOWN, unverified),
        ]
    if kind == "antigravity":
        supported = SupportLevel.SUPPORTED
        unsupported = SupportLevel.UNSUPPORTED
        unverified = VerificationStatus.NOT_TESTED
        return [
            Capability("discover", supported, supported, unverified),
            Capability("create_session", supported, supported, unverified),
            Capability("resume_session", supported, unsupported, unverified),
            Capability("submit", supported, supported, unverified),
            Capability("snapshot_review", SupportLevel.UNKNOWN, unsupported, unverified),
            Capability("observe", supported, supported, unverified),
            Capability("interrupt", supported, supported, unverified),
            Capability("approval", unsupported, unsupported, unverified),
            Capability("native_reference", supported, supported, unverified),
            Capability("usage", supported, supported, unverified),
        ]
    if kind == "claude":
        unverified = VerificationStatus.NOT_TESTED
        return [
            Capability("discover", SupportLevel.SUPPORTED, SupportLevel.SUPPORTED, unverified),
            Capability("create_session", SupportLevel.UNSUPPORTED, SupportLevel.UNSUPPORTED, unverified),
            Capability("resume_session", SupportLevel.UNSUPPORTED, SupportLevel.UNSUPPORTED, unverified),
            Capability("submit", SupportLevel.SUPPORTED, SupportLevel.SUPPORTED, unverified),
            Capability("snapshot_review", SupportLevel.SUPPORTED, SupportLevel.SUPPORTED, unverified),
            Capability("observe", SupportLevel.SUPPORTED, SupportLevel.SUPPORTED, unverified),
            Capability("interrupt", SupportLevel.UNKNOWN, SupportLevel.UNKNOWN, unverified),
            Capability("approval", SupportLevel.UNSUPPORTED, SupportLevel.UNSUPPORTED, unverified),
            Capability("native_reference", SupportLevel.UNKNOWN, SupportLevel.UNKNOWN, unverified),
            Capability("usage", SupportLevel.UNKNOWN, SupportLevel.UNKNOWN, unverified),
        ]
    return [
        Capability(name, SupportLevel.UNKNOWN, SupportLevel.UNKNOWN, VerificationStatus.NOT_TESTED)
        for name in (
            "discover",
            "create_session",
            "resume_session",
            "submit",
            "observe",
            "interrupt",
            "approval",
            "native_reference",
            "usage",
        )
    ]
