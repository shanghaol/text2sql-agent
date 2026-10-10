package com.shanghl.text2sql.service;

import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

import com.shanghl.text2sql.common.BizException;
import com.shanghl.text2sql.common.ErrorCode;

import org.springframework.stereotype.Component;

/**
 * 固定窗口限流（设计 §1.7）：
 * - 对话 10 次/分钟/用户、key 校验 5 次/分钟/用户、登录注册 10 次/分钟/IP（X-Forwarded-For 首值）。
 * - 实现：ConcurrentHashMap 桶 + 固定窗口计数，~40 行。
 *   被否 Bucket4j/Redis：单实例 Java 内存限流已满足单机事实（compose 只有这一个 Java），
 *   Redis 在 2G 服务器上是奢侈品；面试答法备好"生产用 Bucket4j 分布式限流，单机 demo
 *   用固定窗口是成本匹配的选择"。
 * 桶数量 = 活跃 key 数（用户数/IP 数量级），单机 demo 不做后台过期清理；
 * 若上量，应加惰性清理（size 超阈值时按 windowStart 过期删除）。
 * nowMillis 可注入：单测用假时钟验证"窗口过期恢复"（不起 Spring 容器）。
 */
@Component
public class RateLimiter {

    private static final class Bucket {
        final long windowStart;
        final AtomicLong count = new AtomicLong();

        Bucket(long windowStart) {
            this.windowStart = windowStart;
        }
    }

    private final ConcurrentHashMap<String, Bucket> buckets = new ConcurrentHashMap<>();

    /** 生产入口：系统时钟。超限抛 BizException（由调用方决定错误码：rate_limited / verify_rate_limited）。 */
    public void check(String key, ErrorCode exceeded, int limit, long windowMillis) {
        if (!tryAcquire(key, limit, windowMillis, System.currentTimeMillis())) {
            throw new BizException(exceeded);
        }
    }

    public boolean tryAcquire(String key, int limit, long windowMillis) {
        return tryAcquire(key, limit, windowMillis, System.currentTimeMillis());
    }

    /** 固定窗口判定（nowMillis 注入以便测试）：窗口起点 = now - now % windowMillis。 */
    public boolean tryAcquire(String key, int limit, long windowMillis, long nowMillis) {
        long windowStart = nowMillis - (nowMillis % windowMillis);
        Bucket bucket = buckets.compute(key, (k, current) -> {
            if (current == null || current.windowStart != windowStart) {
                return new Bucket(windowStart); // 窗口翻新：计数从 0 开始
            }
            return current;
        });
        // compute 内只做翻新，计数用原子自增：同一窗口内并发自增安全
        long n = bucket.count.incrementAndGet();
        return n <= limit; // 超限的那次也消耗了计数——固定窗口语义下无影响
    }
}
