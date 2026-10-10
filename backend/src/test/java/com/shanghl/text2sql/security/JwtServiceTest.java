package com.shanghl.text2sql.security;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;

import javax.crypto.SecretKey;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import io.jsonwebtoken.Claims;
import io.jsonwebtoken.ExpiredJwtException;
import io.jsonwebtoken.JwtException;
import io.jsonwebtoken.security.Keys;
import io.jsonwebtoken.security.SignatureException;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * JWT 单测（不起 Spring）：签发/校验往返、时钟偏移判定过期、篡改拒绝、typ 混用拒绝。
 */
class JwtServiceTest {

    private static final SecretKey KEY =
            Keys.hmacShaKeyFor("0123456789abcdef0123456789abcdef".getBytes()); // 32B → HS256

    private JwtService service;

    @BeforeEach
    void setUp() {
        service = new JwtService(KEY, Duration.ofHours(2), Duration.ofDays(7), Clock.systemUTC());
    }

    @Test
    @DisplayName("签发→解析往返：sub=userId、username、typ 正确")
    void issueAndParseRoundTrip() {
        String access = service.issueAccess(42L, "alice");
        Claims claims = service.parseAccess(access);
        assertThat(claims.getSubject()).isEqualTo("42");
        assertThat(claims.get("username", String.class)).isEqualTo("alice");
        assertThat(claims.get("typ", String.class)).isEqualTo("access");

        String refresh = service.issueRefresh(42L, "alice");
        assertThat(service.parseRefresh(refresh).get("typ", String.class)).isEqualTo("refresh");
    }

    @Test
    @DisplayName("access 2h 过期：时钟偏移 2h+1min 后解析抛 ExpiredJwtException")
    void expiredAfterClockSkew() {
        String access = service.issueAccess(42L, "alice");
        Clock shifted = Clock.offset(Clock.systemUTC(), Duration.ofHours(2).plusMinutes(1));
        JwtService future = new JwtService(KEY, Duration.ofHours(2), Duration.ofDays(7), shifted);
        assertThatThrownBy(() -> future.parseAccess(access))
                .isInstanceOf(ExpiredJwtException.class);
    }

    @Test
    @DisplayName("refresh 7d 未过期时，偏移 2h 的时钟仍可解析（时长边界）")
    void refreshSurvivesAccessTtlSkew() {
        String refresh = service.issueRefresh(42L, "alice");
        Clock shifted = Clock.offset(Clock.systemUTC(), Duration.ofHours(2));
        JwtService future = new JwtService(KEY, Duration.ofHours(2), Duration.ofDays(7), shifted);
        assertThat(future.parseRefresh(refresh).getSubject()).isEqualTo("42");
    }

    @Test
    @DisplayName("篡改 token → 签名校验拒绝（SignatureException）")
    void tamperedTokenRejected() {
        String access = service.issueAccess(42L, "alice");
        String[] parts = access.split("\\.");
        String sig = parts[2];
        String flipped = sig.startsWith("A") ? "B" + sig.substring(1) : "A" + sig.substring(1);
        String tampered = parts[0] + "." + parts[1] + "." + flipped;
        assertThatThrownBy(() -> service.parseAccess(tampered))
                .isInstanceOf(JwtException.class)
                .isInstanceOf(SignatureException.class);
    }

    @Test
    @DisplayName("typ 混用拒绝：refresh token 不能当 access 用（反之亦然）")
    void typMismatchRejected() {
        String refresh = service.issueRefresh(42L, "alice");
        String access = service.issueAccess(42L, "alice");
        assertThatThrownBy(() -> service.parseAccess(refresh)).isInstanceOf(JwtException.class);
        assertThatThrownBy(() -> service.parseRefresh(access)).isInstanceOf(JwtException.class);
        // 用错误密钥签的 token 也拒绝
        SecretKey otherKey = Keys.hmacShaKeyFor("ffffffffffffffffffffffffffffffff".getBytes());
        JwtService forged = new JwtService(otherKey, Duration.ofHours(2), Duration.ofDays(7),
                Clock.fixed(Instant.parse("2026-10-11T00:00:00Z"), ZoneOffset.UTC));
        String foreign = forged.issueAccess(42L, "alice");
        assertThatThrownBy(() -> service.parseAccess(foreign)).isInstanceOf(JwtException.class);
    }

    @Test
    @DisplayName("畸形 token（乱串）→ JwtException，不抛出意外运行时异常")
    void malformedTokenRejected() {
        assertThatThrownBy(() -> service.parseAccess("not-a-jwt")).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> service.parseAccess("a.b.c")).isInstanceOf(JwtException.class);
    }
}
