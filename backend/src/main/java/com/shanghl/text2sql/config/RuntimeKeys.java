package com.shanghl.text2sql.config;

import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.time.Clock;
import java.util.Arrays;
import java.util.Base64;

import javax.crypto.SecretKey;
import javax.crypto.spec.SecretKeySpec;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Bean;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import jakarta.annotation.PostConstruct;

/**
 * 运行期密钥材料：BYOK 主密钥（AES-256-GCM）与 JWT 签名密钥（HS256）。
 *
 * 启动语义（设计 §1.4，注释即验收依据）：
 * - 生产（无 dev profile）：JWT_SECRET / BYOK_MASTER_KEY 缺失或非法 → 抛异常 → 启动失败（fail-fast）。
 *   缺主密钥的 BYOK 应用等于裸奔，不许带病运行；JWT_SECRET < 32 字节不满足 HS256 强度要求。
 * - dev（--spring.profiles.active=dev）：缺失时生成临时随机值并打 WARN，方便没配 .env 也能联调。
 *   ——生产 fail-fast / dev 宽松是刻意保留的语义分歧，不是遗漏：
 *     dev 下临时主密钥意味着「重启后旧密文不可解（AEADBadTagException→主密钥不匹配）、
 *     重启后旧签发的 token 全部失效」，仅限本机开发可接受；生产上这两种现象都等于事故。
 */
@Component
public class RuntimeKeys {

    private static final Logger log = LoggerFactory.getLogger(RuntimeKeys.class);
    private static final int MASTER_KEY_BYTES = 32; // 256 位
    private static final int JWT_SECRET_MIN_BYTES = 32; // HS256 要求 ≥256 位

    private final AppProperties props;
    private final Environment env;
    private final SecureRandom secureRandom = new SecureRandom();

    private SecretKeySpec byokMasterKey;
    private SecretKey jwtHmacKey;
    private boolean devTemporaryKeys;

    public RuntimeKeys(AppProperties props, Environment env) {
        this.props = props;
        this.env = env;
    }

    @PostConstruct
    void init() {
        boolean dev = Arrays.asList(env.getActiveProfiles()).contains("dev");
        String jwtSecret = props.getJwt().getSecret();
        String masterKeyB64 = props.getByok().getMasterKey();

        if (isBlank(jwtSecret) || isBlank(masterKeyB64)) {
            if (!dev) {
                // 生产 fail-fast（§1.4）：下面这条异常会让 Spring 启动直接失败
                throw new IllegalStateException(
                        "缺少必需环境变量：JWT_SECRET 与 BYOK_MASTER_KEY 必须配置"
                                + "（生成：openssl rand -base64 32；生产缺失即退出，不允许带病运行）。"
                                + "本机联调请使用 --spring.profiles.active=dev。");
            }
            jwtSecret = randomBase64();
            masterKeyB64 = randomBase64();
            devTemporaryKeys = true;
            log.warn("JWT_SECRET / BYOK_MASTER_KEY 未注入（dev profile）——本次启动生成临时随机值。"
                    + "重启后旧密文不可解、旧 token 失效；生产环境缺失将 fail-fast。");
        }

        byte[] jwtBytes = jwtSecret.getBytes(StandardCharsets.UTF_8);
        if (jwtBytes.length < JWT_SECRET_MIN_BYTES) {
            throw new IllegalStateException("JWT_SECRET 长度不足：" + jwtBytes.length
                    + " 字节，HS256 要求至少 " + JWT_SECRET_MIN_BYTES + " 字节。");
        }

        byte[] masterBytes;
        try {
            masterBytes = Base64.getDecoder().decode(masterKeyB64);
        } catch (IllegalArgumentException e) {
            throw new IllegalStateException("BYOK_MASTER_KEY 不是合法 Base64（生成：openssl rand -base64 32）。", e);
        }
        if (masterBytes.length != MASTER_KEY_BYTES) {
            throw new IllegalStateException("BYOK_MASTER_KEY 解码后为 " + masterBytes.length
                    + " 字节，必须恰好 32 字节（256 位）。");
        }

        this.jwtHmacKey = Keys.hmacShaKeyFor(jwtBytes);
        this.byokMasterKey = new SecretKeySpec(masterBytes, "AES");
        log.info("密钥材料就绪（dev 临时密钥={}，jwt secret 字节数={}，master key 字节数={}）",
                devTemporaryKeys, jwtBytes.length, masterBytes.length);
    }

    /** BYOK 主密钥（AES-256-GCM 用）。交给 KeyCipherService，加密点在那里显式可见（§1.3）。 */
    @Bean
    public SecretKeySpec byokMasterKey() {
        return byokMasterKey;
    }

    /** JWT 签名密钥（HS256）。 */
    @Bean
    public SecretKey jwtHmacKey() {
        return jwtHmacKey;
    }

    /** 应用统一时钟（JwtService 签发/校验共用；单测注入偏移时钟验证过期）。 */
    @Bean
    public Clock appClock() {
        return Clock.systemUTC();
    }

    public boolean isDevTemporaryKeys() {
        return devTemporaryKeys;
    }

    private static boolean isBlank(String s) {
        return s == null || s.isBlank();
    }

    private String randomBase64() {
        byte[] buf = new byte[MASTER_KEY_BYTES];
        secureRandom.nextBytes(buf);
        return Base64.getEncoder().encodeToString(buf);
    }

    /** 仅供诊断/测试使用：确认 parser 可用（防止 jjwt 版本 API 漂移未被发现）。 */
    static void smokeParserApi() {
        Jwts.parser();
    }
}
