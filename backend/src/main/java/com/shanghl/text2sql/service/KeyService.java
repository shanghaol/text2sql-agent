package com.shanghl.text2sql.service;

import java.time.LocalDateTime;
import java.util.regex.Pattern;

import com.baomidou.mybatisplus.core.toolkit.Wrappers;
import com.shanghl.text2sql.common.BizException;
import com.shanghl.text2sql.common.ErrorCode;
import com.shanghl.text2sql.dto.KeyPutResult;
import com.shanghl.text2sql.dto.KeyStatus;
import com.shanghl.text2sql.dto.VerifyKeyResult;
import com.shanghl.text2sql.entity.ApiKey;
import com.shanghl.text2sql.llm.DeepSeekVerifyClient;
import com.shanghl.text2sql.mapper.ApiKeyMapper;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

/**
 * BYOK 密钥管理（设计 §1.2 录入五步 / §1.3 存储 / §1.5 日志脱敏 / §2.4 四接口）。
 *
 * 录入五步（PUT /api/user/key）：
 *   ① 格式预检 ^sk-[0-9a-zA-Z]{20,}$（防绕过前端）→ 400 bad_key_format；
 *   ② 同步调 DeepSeek GET /models 校验（5s 超时，免费幂等）；
 *   ③ 校验通过 → AES-GCM 加密 → upsert api_key 行（一人一把，重复录入走更新、IV 重新随机）；
 *   ④ 校验失败 → **不落库**（状态机里没有"已存的无效 key"这种状态）→ 400 key_invalid；
 *   ⑤ 返回掩码 + 指纹 + 校验时间（前端展示"已配置：sk-****ab3d（指纹 1a2b3c4d…）"）。
 *
 * 日志纪律（§1.5 规则 1/2/9）：本类任何日志只出现 fp=<指纹> 与 len=<长度>，
 * 绝不打请求体、不打掩码、不打明文——grep "sk-" 必须为 0。
 */
@Service
public class KeyService {

    /** 与前端同构的宽松格式（KeyPutRequest 的 @Pattern 之外，服务层再查一道防绕过）。 */
    static final Pattern KEY_FORMAT = Pattern.compile("^sk-[0-9a-zA-Z]{20,}$");

    private static final Logger log = LoggerFactory.getLogger(KeyService.class);

    private final ApiKeyMapper apiKeyMapper;
    private final KeyCipherService cipher;
    private final DeepSeekVerifyClient verifyClient;
    private final RateLimiter rateLimiter;
    private final int keyVerifyPerMin;

    public KeyService(ApiKeyMapper apiKeyMapper, KeyCipherService cipher,
                      DeepSeekVerifyClient verifyClient, RateLimiter rateLimiter,
                      com.shanghl.text2sql.config.AppProperties props) {
        this.apiKeyMapper = apiKeyMapper;
        this.cipher = cipher;
        this.verifyClient = verifyClient;
        this.rateLimiter = rateLimiter;
        this.keyVerifyPerMin = props.getChat().getKeyVerifyRateLimitPerMin();
    }

    public KeyPutResult put(long userId, String rawKey) {
        // ① 格式预检（防绕过前端；KeyPutRequest 的 @Pattern 已拦一道，这里显式再查）
        if (rawKey == null || !KEY_FORMAT.matcher(rawKey).matches()) {
            throw new BizException(ErrorCode.BAD_KEY_FORMAT);
        }
        // 限流 5/min：每次校验是一次出站 DeepSeek 调用，防被当代理用（§1.7）
        rateLimiter.check("keyverify:" + userId, ErrorCode.VERIFY_RATE_LIMITED,
                keyVerifyPerMin, 60_000L);
        // ② 同步校验 GET /models（§1.2：401→key_invalid；429→verify_rate_limited；超时→verify_unreachable）
        DeepSeekVerifyClient.VerifyOutcome outcome = verifyClient.verify(rawKey);
        switch (outcome) {
            case VALID -> { /* 走落库 */ }
            case INVALID -> {
                // ④ 校验失败不落库
                log.info("key put rejected userId={} outcome={}（未落库）", userId, outcome);
                throw new BizException(ErrorCode.KEY_INVALID);
            }
            case RATE_LIMITED -> throw new BizException(ErrorCode.VERIFY_RATE_LIMITED);
            case UNREACHABLE -> throw new BizException(ErrorCode.VERIFY_UNREACHABLE);
        }
        // ③ AES-GCM 加密 upsert：IV 重新随机生成（KeyCipherService 内显式加密点）
        KeyCipherService.SealedKey sealed = cipher.encrypt(rawKey);
        String fingerprint = KeyCipherService.fingerprint(rawKey);
        String masked = KeyCipherService.mask(rawKey);
        LocalDateTime now = LocalDateTime.now();

        ApiKey row = apiKeyMapper.selectOne(Wrappers.<ApiKey>lambdaQuery().eq(ApiKey::getUserId, userId));
        if (row == null) {
            row = new ApiKey();
            row.setUserId(userId);
            row.setKeyCiphertext(sealed.ciphertext());
            row.setKeyIv(sealed.iv());
            row.setKeyFingerprint(fingerprint);
            row.setKeyMasked(masked);
            row.setKeyVersion(1);
            row.setLastVerifiedAt(now);
            apiKeyMapper.insert(row);
        } else {
            row.setKeyCiphertext(sealed.ciphertext());
            row.setKeyIv(sealed.iv());
            row.setKeyFingerprint(fingerprint);
            row.setKeyMasked(masked);
            row.setLastVerifiedAt(now);
            apiKeyMapper.updateById(row);
        }
        // §1.5 规则 2：key 的唯一日志替身 = 指纹 + 长度
        log.info("key saved userId={} fp={} len={}", userId, fingerprint, rawKey.length());
        return new KeyPutResult(masked, fingerprint, now);
    }

    /** GET /api/user/key：只回掩码/指纹/时间，永不回明文（§2.4）。 */
    public KeyStatus get(long userId) {
        ApiKey row = byUser(userId);
        if (row == null) {
            return new KeyStatus(false, null, null, null);
        }
        return new KeyStatus(true, row.getKeyMasked(), row.getKeyFingerprint(), row.getLastVerifiedAt());
    }

    /** POST /api/user/key/verify：对已存 key 重跑 /models（设置页"重新校验"；5 次/分限流）。 */
    public VerifyKeyResult verifyStored(long userId) {
        ApiKey row = byUser(userId);
        if (row == null) {
            throw new BizException(ErrorCode.KEY_NOT_SET);
        }
        rateLimiter.check("keyverify:" + userId, ErrorCode.VERIFY_RATE_LIMITED,
                keyVerifyPerMin, 60_000L);
        // 解密（微秒级，不缓存明文）；AEADBadTag → 主密钥不匹配（§1.3 / §7 风险 6）
        String plaintext = decryptOrThrow(row);
        DeepSeekVerifyClient.VerifyOutcome outcome = verifyClient.verify(plaintext);
        if (outcome == DeepSeekVerifyClient.VerifyOutcome.VALID) {
            row.setLastVerifiedAt(LocalDateTime.now());
            apiKeyMapper.updateById(row);
            log.info("key re-verified userId={} fp={}", userId, row.getKeyFingerprint());
            return new VerifyKeyResult(true, row.getLastVerifiedAt());
        }
        // 无效/限流/不可达都以 {valid:false} 或对应错误码返回——已存行不动，用户可重录
        if (outcome == DeepSeekVerifyClient.VerifyOutcome.RATE_LIMITED) {
            throw new BizException(ErrorCode.VERIFY_RATE_LIMITED);
        }
        if (outcome == DeepSeekVerifyClient.VerifyOutcome.UNREACHABLE) {
            throw new BizException(ErrorCode.VERIFY_UNREACHABLE);
        }
        log.info("key re-verify invalid userId={} fp={}", userId, row.getKeyFingerprint());
        return new VerifyKeyResult(false, null);
    }

    /** DELETE /api/user/key：硬删行；删除后对话返回 key_not_set（§2.4）。 */
    public void delete(long userId) {
        ApiKey row = byUser(userId);
        if (row != null) {
            // 日志只打指纹（规则 2）
            log.info("key deleted userId={} fp={}", userId, row.getKeyFingerprint());
            apiKeyMapper.deleteById(row.getId());
        }
    }

    ApiKey byUser(long userId) {
        return apiKeyMapper.selectOne(Wrappers.<ApiKey>lambdaQuery().eq(ApiKey::getUserId, userId));
    }

    /** 显式解密点（对话代理同样走这里）：AEADBadTagException → 业务错误"主密钥不匹配"。 */
    public String decryptOrThrow(ApiKey row) {
        try {
            return cipher.decrypt(row.getKeyIv(), row.getKeyCiphertext());
        } catch (javax.crypto.AEADBadTagException e) {
            log.error("decrypt failed userId={} fp={}（主密钥不匹配或密文损坏）", row.getUserId(), row.getKeyFingerprint());
            throw new BizException(ErrorCode.MASTER_KEY_MISMATCH);
        }
    }
}
