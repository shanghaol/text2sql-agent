package com.shanghl.text2sql.security;

import java.time.Clock;
import java.time.Duration;
import java.util.Date;

import javax.crypto.SecretKey;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

import com.shanghl.text2sql.config.AppProperties;

import io.jsonwebtoken.Claims;
import io.jsonwebtoken.JwtException;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.MalformedJwtException;
import io.jsonwebtoken.security.Keys;

/**
 * JWT 签发与校验（设计 §2.3）：HS256 双 token、无状态。
 * - access token（2h）：claims sub=userId, username, typ=access
 * - refresh token（7d）：同密钥不同 typ=refresh
 *
 * 已知限制（§2.3 写明）：无状态 refresh 无法服务端吊销——登出=客户端丢弃，
 * token 在剩余有效期内仍有效；需要吊销时正确做法是 refresh token 落库（需第 5 张表，超范围）。
 * 应急手段：整体轮换 JWT_SECRET 强制全员重新登录。
 *
 * Clock 可注入：过期判断随注入时钟走，单测用时钟偏移验证过期（不起 Spring 容器）。
 */
@Component
public class JwtService {

    public static final String TYP_ACCESS = "access";
    public static final String TYP_REFRESH = "refresh";

    private final SecretKey key;
    private final Duration accessTtl;
    private final Duration refreshTtl;
    private final Clock clock;

    /** Spring 装配入口：TTL 来自 AppProperties，时钟为全局 appClock。 */
    @Autowired
    public JwtService(SecretKey jwtHmacKey, AppProperties props, Clock clock) {
        this(jwtHmacKey, props.getJwt().getAccessTtl(), props.getJwt().getRefreshTtl(), clock);
    }

    public JwtService(SecretKey jwtHmacKey, Duration accessTtl, Duration refreshTtl, Clock clock) {
        this.key = jwtHmacKey;
        this.accessTtl = accessTtl;
        this.refreshTtl = refreshTtl;
        this.clock = clock;
    }

    public String issueAccess(long userId, String username) {
        return issue(userId, username, TYP_ACCESS, accessTtl);
    }

    public String issueRefresh(long userId, String username) {
        return issue(userId, username, TYP_REFRESH, refreshTtl);
    }

    private String issue(long userId, String username, String typ, Duration ttl) {
        return Jwts.builder()
                .subject(Long.toString(userId))
                .claim("username", username)
                .claim("typ", typ)
                .issuedAt(Date.from(clock.instant()))
                .expiration(Date.from(clock.instant().plus(ttl)))
                .signWith(key) // hmacShaKeyFor(32B) → HS256 自动选择
                .compact();
    }

    /** 解析并校验签名、有效期与 typ；任何不满足都抛 JwtException（子类区分过期/篡改/格式）。 */
    public Claims parse(String token, String expectedTyp) {
        Claims claims = Jwts.parser()
                .verifyWith(key)
                .clock(() -> Date.from(clock.instant())) // 与签发共用同一时钟，便于测试偏移
                .build()
                .parseSignedClaims(token)
                .getPayload();
        String typ = claims.get("typ", String.class);
        if (!expectedTyp.equals(typ)) {
            // 用 access token 调 refresh、或反之——按无效 token 处理
            throw new MalformedJwtException("token typ 不匹配: 期望 " + expectedTyp);
        }
        return claims;
    }

    public Claims parseAccess(String token) {
        return parse(token, TYP_ACCESS);
    }

    public Claims parseRefresh(String token) {
        return parse(token, TYP_REFRESH);
    }
}
