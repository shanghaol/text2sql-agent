package com.shanghl.text2sql.service;

import com.baomidou.mybatisplus.core.toolkit.Wrappers;
import com.shanghl.text2sql.common.BizException;
import com.shanghl.text2sql.common.ErrorCode;
import com.shanghl.text2sql.entity.Feedback;
import com.shanghl.text2sql.entity.QueryHistory;
import com.shanghl.text2sql.mapper.FeedbackMapper;
import com.shanghl.text2sql.mapper.QueryHistoryMapper;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

/**
 * 反馈 upsert（设计 §2.4：POST /api/feedback，唯一键 uk_feedback_history，重复点击走更新）。
 * 越权二次校验（§1.7）：history 必须存在且属于当前用户，否则 history_not_found。
 */
@Service
public class FeedbackService {

    private static final Logger log = LoggerFactory.getLogger(FeedbackService.class);

    private final FeedbackMapper feedbackMapper;
    private final QueryHistoryMapper historyMapper;

    public FeedbackService(FeedbackMapper feedbackMapper, QueryHistoryMapper historyMapper) {
        this.feedbackMapper = feedbackMapper;
        this.historyMapper = historyMapper;
    }

    public void upsert(long userId, long historyId, int rating, String comment) {
        QueryHistory history = historyMapper.selectById(historyId);
        if (history == null || history.getUserId() == null || history.getUserId() != userId) {
            throw new BizException(ErrorCode.HISTORY_NOT_FOUND);
        }
        Feedback existing = feedbackMapper.selectOne(
                Wrappers.<Feedback>lambdaQuery().eq(Feedback::getHistoryId, historyId));
        if (existing == null) {
            Feedback f = new Feedback();
            f.setHistoryId(historyId);
            f.setUserId(userId);
            f.setRating(rating);
            f.setComment(comment);
            feedbackMapper.insert(f);
        } else {
            existing.setRating(rating);
            existing.setComment(comment);
            feedbackMapper.updateById(existing);
        }
        log.info("feedback upserted userId={} historyId={} rating={}", userId, historyId, rating);
    }
}
