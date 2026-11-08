/*
 * Copyright (c) 2025 OceanBase.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#define USING_LOG_PREFIX STORAGE

#include "ob_text_daat_iter.h"

namespace oceanbase
{
namespace storage
{
int ObTextDaaTIter::init(const ObTextDaaTParam &param)
{
  int ret = OB_SUCCESS;
  if (OB_ISNULL(param.base_param_) || OB_ISNULL(param.dim_iters_) || OB_ISNULL(param.allocator_)
      || OB_ISNULL(param.relevance_collector_)) {
    ret = OB_INVALID_ARGUMENT;
  } else if (OB_FAIL(ObSRDaaTIterImpl::init(*param.base_param_, *param.dim_iters_,
                                            *param.allocator_, *param.relevance_collector_,
                                            true /* enable_integer_buffer */))) {
  } else if (param.base_param_->need_project_relevance()
      && OB_FAIL(bm25_param_estimator_.init(param.bm25_param_est_ctx_))) {
  } else {
    mode_flag_ = param.mode_flag_;
    function_lookup_mode_ = param.function_lookup_mode_;
    // For a few score-free posting lists, selecting the smallest head
    // avoids maintaining a sorted heap and retrieving a zero score per row.
    // Use the same bound as the generic simple merger, not a query-size knob.
    use_small_merge_ = mode_flag_ == sql::ObMatchAgainstMode::NATURAL_LANGUAGE_MODE
        && !function_lookup_mode_
        && !iter_param_->need_project_relevance()
        && nullptr == iter_param_->relevance_expr_
        && !iter_param_->need_filter()
        && !iter_param_->need_pushdown_topk()
        && nullptr == iter_param_->dim_weights_
        && dim_iters_->count() > 0
        && dim_iters_->count() <= ObSRSimpleMerger::USE_SIMPLE_MERGER_MAX_TABLE_CNT;
    unsigned_integer_id_ = iter_param_->id_proj_expr_->datum_meta_.type_ == common::ObUInt64Type;
  }
  return ret;
}

void ObTextDaaTIter::reset()
{
  use_small_merge_ = false;
  unsigned_integer_id_ = false;
  bm25_param_estimator_.reset();
  // this class can know the type of dim_iters_, but ObSRDaaTIterImpl maybe not
  if (OB_NOT_NULL(dim_iters_)) {
    for (int64_t i = 0; i < dim_iters_->count(); ++i) {
      static_cast<ObTextRetrievalDaaTTokenIter *>(dim_iters_->at(i))->reset();
    }
  }
  ObSRDaaTIterImpl::reset();
}

void ObTextDaaTIter::reuse(const bool switch_tablet)
{
  if ((OB_NOT_NULL(dim_iters_) && 0 == dim_iters_->count())) {
    // do nothing
  } else {
    bm25_param_estimator_.reuse(switch_tablet);
  }
  if (OB_NOT_NULL(dim_iters_)) {
    for (int64_t i = 0; i < dim_iters_->count(); ++i) {
      static_cast<ObTextRetrievalDaaTTokenIter *>(dim_iters_->at(i))->reuse();
    }
  }
  ObSRDaaTIterImpl::reuse(switch_tablet);
}

int ObTextDaaTIter::pre_process()
{
  int ret = OB_SUCCESS;
  if (dim_iters_->count() == 0) {
    ret = OB_ITER_END;
  } else if (iter_param_->need_project_relevance()) {
    if (OB_FAIL(bm25_param_estimator_.do_estimation(*iter_param_->eval_ctx_))) {
    }
  }
  return ret;
}

int ObTextDaaTIter::do_one_merge_round(int64_t &count)
{
  int ret = OB_SUCCESS;
  if (!use_small_merge_) {
    ret = ObSRDaaTIterImpl::do_one_merge_round(count);
  } else {
    for (int64_t i = 0; OB_SUCC(ret) && i < next_round_cnt_; ++i) {
      const int64_t iter_idx = next_round_iter_idxes_[i];
      ObTextRetrievalDaaTTokenIter *token_iter =
          static_cast<ObTextRetrievalDaaTTokenIter *>(dim_iter_ptr_cache_[iter_idx]);
      if (OB_ISNULL(token_iter)) {
        ret = OB_ERR_UNEXPECTED;
      } else if (OB_FAIL(token_iter->get_next_row())) {
        if (OB_ITER_END == ret) {
          iter_domain_ids_[iter_idx] = nullptr;
          ret = OB_SUCCESS;
        }
      } else if (OB_FAIL(token_iter->get_curr_id(iter_domain_ids_[iter_idx]))) {
      }
    }

    if (OB_SUCC(ret)) {
      next_round_cnt_ = 0;
      const ObDatum *min_id = nullptr;
      ObSRMergeItem min_item;
      const int64_t dim_count = dim_iter_ptr_cache_.count();
      for (int64_t i = 0; OB_SUCC(ret) && i < dim_count; ++i) {
        const ObDatum *id = iter_domain_ids_[i];
        if (nullptr != id) {
          int64_t cmp_ret = -1;
          if (nullptr == min_id) {
          } else if (value_id_fast_) {
            cmp_ret = unsigned_integer_id_
                ? (id->get_uint() > min_id->get_uint()) - (id->get_uint() < min_id->get_uint())
                : (id->get_int() > min_id->get_int()) - (id->get_int() < min_id->get_int());
          } else {
            ObSRMergeItem item;
            item.iter_idx_ = i;
            // Preserve the existing type/collation and error semantics for
            // generated document IDs and other non-integer representations.
            ret = merge_cmp_.cmp(item, min_item, cmp_ret);
          }
          if (OB_SUCC(ret) && cmp_ret <= 0) {
            if (cmp_ret < 0) {
              next_round_cnt_ = 0;
            }
            next_round_iter_idxes_[next_round_cnt_++] = i;
            // The simple merger visits equal IDs in iterator-index order;
            // retain its last equal datum as the output representative.
            min_id = id;
            min_item.iter_idx_ = i;
          }
        }
      }
      if (OB_FAIL(ret)) {
      } else if (nullptr == min_id) {
        ret = OB_ITER_END;
      } else {
        // Domain IDs are non-null keys. Each token iterator owns its head
        // datum until that iterator advances again, in the next merge round.
        // Keep the collector so minimum_should_match still counts every term.
        for (int64_t i = 0; OB_SUCC(ret) && i < next_round_cnt_; ++i) {
          if (OB_FAIL(relevance_collector_->collect_one_dim(next_round_iter_idxes_[i], 0.0))) {
          }
        }
        double relevance = 0.0;
        bool need_project = false;
        if (OB_SUCC(ret) && OB_FAIL(relevance_collector_->get_result(relevance, need_project))) {
        } else if (OB_SUCC(ret) && need_project) {
          // Cache before advancing any input. Batch-end projection in the
          // base class protects output from shared scan-expression refills.
          ret = ObSRDaaTIterImpl::cache_result(count, *min_id, relevance);
        }
      }
    }
  }
  return ret;
}

ObTextBMWIter::ObTextBMWIter()
  : ObSRBMWIterImpl(),
    bm25_param_estimator_() {}

void ObTextBMWIter::reuse(const bool switch_tablet)
{
  bm25_param_estimator_.reuse(switch_tablet);
  if (OB_NOT_NULL(dim_iters_)) {
    for (int64_t i = 0; i < dim_iters_->count(); ++i) {
      static_cast<ObTextRetrievalBlockMaxIter *>(dim_iters_->at(i))->reuse();
    }
  }
  ObSRBMWIterImpl::reuse(switch_tablet);
}

void ObTextBMWIter::reset()
{
  bm25_param_estimator_.reset();
  if (OB_NOT_NULL(dim_iters_)) {
    for (int64_t i = 0; i < dim_iters_->count(); ++i) {
      static_cast<ObTextRetrievalBlockMaxIter *>(dim_iters_->at(i))->reset();
    }
  }
  ObSRBMWIterImpl::reset();
}

int ObTextBMWIter::init(const ObTextDaaTParam &param)
{
  int ret = OB_SUCCESS;
  if (OB_ISNULL(param.base_param_) || OB_ISNULL(param.dim_iters_) || OB_ISNULL(param.allocator_)
      || OB_ISNULL(param.relevance_collector_)) {
    ret = OB_INVALID_ARGUMENT;
  } else if (OB_FAIL(ObSRBMWIterImpl::init(*param.base_param_, *param.dim_iters_,
                                           *param.allocator_, *param.relevance_collector_))) {
  } else if (OB_FAIL(bm25_param_estimator_.init(param.bm25_param_est_ctx_))) {
  }
  return ret;
}

int ObTextBMWIter::get_next_rows(const int64_t capacity, int64_t &count)
{
  int ret = OB_SUCCESS;
  if (IS_NOT_INIT) {
    ret = OB_NOT_INIT;
  } else if (dim_iters_->count() == 0) {
    ret = OB_ITER_END;
  } else if (OB_FAIL(bm25_param_estimator_.do_estimation(*iter_param_->eval_ctx_))) {
  }

  if (FAILEDx(ObSRBMWIterImpl::get_next_rows(capacity, count))) {
    if (OB_UNLIKELY(OB_ITER_END != ret)) {
    }
  }
  return ret;
}

int ObTextBMWIter::init_before_wand_process()
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(!bm25_param_estimator_.is_estimated())) {
    ret = OB_ERR_UNEXPECTED;
  }

  for (int64_t i = 0; OB_SUCC(ret) && i < dim_iters_->count(); ++i) {
    ObTextRetrievalBlockMaxIter *block_max_iter = static_cast<ObTextRetrievalBlockMaxIter *>(dim_iters_->at(i));
    if (OB_FAIL(block_max_iter->init_block_max_iter(
        bm25_param_estimator_.get_total_doc_cnt(), bm25_param_estimator_.get_avg_doc_token_cnt()))) {
    }
  }
  return ret;
}

} // namespace storage
} // namespace oceanbase
