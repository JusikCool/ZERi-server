"""비밀번호 정책. NIST SP 800-63B 가이드 기반.

- 최소 8자 (이미 Pydantic Field에서 강제)
- 흔한 비밀번호 차단
- 이메일 local-part / 사용자 이름과 동일 차단
- 단일 문자 반복 차단 (aaaaaaaa, 11111111 등)

zxcvbn 같은 풀 분석은 의도적으로 제외 — 의존성 부담 + 한글/한국어 컨텍스트에 약함.
"""

from __future__ import annotations

# 흔한 약한 비밀번호 (lowercase 비교). 운영 진입 시 더 큰 리스트로 교체 가능.
_COMMON_PASSWORDS: frozenset[str] = frozenset(
    {
        "password",
        "password1",
        "password12",
        "password123",
        "password1234",
        "12345678",
        "123456789",
        "1234567890",
        "qwerty123",
        "qwertyuiop",
        "asdfghjkl",
        "zxcvbnm",
        "iloveyou",
        "letmein",
        "welcome1",
        "admin123",
        "passw0rd",
        "p@ssw0rd",
        "monkey123",
        "qweasd123",
        "11111111",
        "00000000",
        "abcd1234",
        "abcdefgh",
        "test1234",
        "before123",
        "user1234",
    }
)


class PasswordPolicyError(ValueError):
    """비밀번호가 정책을 위반함. ErrorCode.INVALID_PARAMETER로 매핑."""


def validate_password(password: str, *, email: str | None = None, name: str | None = None) -> None:
    """위반 시 PasswordPolicyError raise. 통과면 None."""
    pw_lower = password.lower()

    if pw_lower in _COMMON_PASSWORDS:
        raise PasswordPolicyError("자주 사용되는 비밀번호는 사용할 수 없습니다.")

    # 단일 문자 반복: aaaaaaaa, 11111111
    if len(set(password)) == 1:
        raise PasswordPolicyError("같은 문자만으로 이루어진 비밀번호는 사용할 수 없습니다.")

    if email:
        local = email.split("@", 1)[0].lower()
        if len(local) >= 3 and local in pw_lower:
            raise PasswordPolicyError("이메일과 유사한 비밀번호는 사용할 수 없습니다.")

    if name:
        name_lower = name.lower()
        if len(name_lower) >= 3 and name_lower in pw_lower:
            raise PasswordPolicyError("이름과 유사한 비밀번호는 사용할 수 없습니다.")


__all__ = ["validate_password", "PasswordPolicyError"]
