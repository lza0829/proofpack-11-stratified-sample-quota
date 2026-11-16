<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJSON, postJSON } from '../api'

const subjects = ref([])
const batches = ref([])
const quotas = ref([])
const subjectId = ref(null)
const seed = ref('核证匣-默认种子')
const last = ref(null)
const err = ref('')
const busy = ref(false)

const STATUS = { ready: '就绪', ready_shortfall: '就绪·未满' }

const currentQuota = computed(
  () => quotas.value.find((q) => q.subject_id === Number(subjectId.value)) || null,
)

function quotaText(q) {
  if (!q || !q.current) return ''
  const c = q.current
  const layers = Object.entries(c.quotas)
    .map(([k, v]) => `${k} ${c.quota_mode === 'count' ? v + ' 条' : Math.round(v * 100) + '%'}`)
    .join(' · ')
  const policy = c.shortfall_policy === 'strict' ? '严格' : '尽力'
  return `v${c.version} · ${layers} · 不足配额：${policy}`
}

async function refresh() {
  subjects.value = await getJSON('/api/subjects')
  batches.value = await getJSON('/api/samples')
  quotas.value = await getJSON('/api/quota')
  if (!subjectId.value && subjects.value.length) subjectId.value = subjects.value[0].id
}

onMounted(async () => {
  try { await refresh() } catch (e) { err.value = String(e.message || e) }
})

async function run() {
  err.value = ''
  last.value = null
  busy.value = true
  try {
    last.value = await postJSON('/api/samples/run', {
      subject_id: Number(subjectId.value),
      seed: seed.value,
    })
    batches.value = await getJSON('/api/samples')
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}
</script>
<template>
  <div class="page">
    <h1>采样台</h1>
    <p class="muted">按夹具约定的分层字段与层配额逐层确定性抽样，总样本为各层合并；批次写明每层源条数、样本数、是否触达配额。</p>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <div class="panel">
      <h3>执行分层采样</h3>
      <div class="row">
        <div class="field">
          <label>举证对象</label>
          <select v-model="subjectId">
            <option v-for="s in subjects" :key="s.id" :value="s.id">{{ s.title }}</option>
          </select>
        </div>
        <div class="field">
          <label>种子</label>
          <input v-model="seed" class="mono" style="min-width:12rem" />
        </div>
        <button :disabled="busy || !subjectId" @click="run">执行采样</button>
      </div>
      <p v-if="currentQuota" class="muted" style="margin-top:0.5rem">
        当前层配额（分层字段 <span class="mono">{{ currentQuota.strata_field }}</span>）：
        {{ quotaText(currentQuota) }}
        <router-link to="/settings">去设置调整 →</router-link>
      </p>
      <div v-if="last" style="margin-top:0.75rem">
        <p>
          批次 <span class="mono">{{ last.batch_no }}</span>
          · 配额 <span class="tag">v{{ last.quota_version }}</span>
          · 源 <strong>{{ last.source_count }}</strong>
          · 样本 <strong>{{ last.sample_count }}</strong>
          · <span class="tag" :class="last.quota_met ? 'ok' : 'danger'">{{ last.quota_met ? '各层达额' : '存在未满层' }}</span>
        </p>
        <table style="margin-top:0.5rem">
          <thead>
            <tr><th>层</th><th>源条数</th><th>目标条数</th><th>样本数</th><th>触达配额</th></tr>
          </thead>
          <tbody>
            <tr v-for="s in last.strata" :key="s.stratum">
              <td>{{ s.stratum }}</td>
              <td>{{ s.source_count }}</td>
              <td>{{ s.target_count }}</td>
              <td>{{ s.sample_count }}</td>
              <td><span class="tag" :class="s.quota_met ? 'ok' : 'danger'">{{ s.quota_met ? '达额' : '未满' }}</span></td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
    <div class="panel">
      <h3>采样批次</h3>
      <table>
        <thead>
          <tr>
            <th>批次号</th>
            <th>对象</th>
            <th>种子</th>
            <th>配额版本</th>
            <th>源条数</th>
            <th>样本数</th>
            <th>分层达额</th>
            <th>状态</th>
            <th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="b in batches" :key="b.id">
            <td class="mono">{{ b.batch_no }}</td>
            <td>{{ b.subject_title }}</td>
            <td class="mono">{{ b.seed }}</td>
            <td><span v-if="b.quota_version" class="tag">v{{ b.quota_version }}</span><span v-else class="muted">旧平采</span></td>
            <td>{{ b.source_count }}</td>
            <td>{{ b.sample_count }}</td>
            <td>
              <template v-if="b.strata && b.strata.length">
                <span v-for="s in b.strata" :key="s.stratum" class="tag" :class="s.quota_met ? 'ok' : 'danger'" style="margin-right:0.25rem">
                  {{ s.stratum }} {{ s.sample_count }}/{{ s.target_count }}
                </span>
              </template>
              <span v-else class="muted">无分层明细</span>
            </td>
            <td><span class="tag" :class="b.quota_met === 0 ? 'danger' : ''">{{ STATUS[b.status] || b.status }}</span></td>
            <td class="mono">{{ b.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
