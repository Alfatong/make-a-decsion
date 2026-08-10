<template>
  <div v-loading="loading">
    <el-card v-if="ch">
      <template #header>
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span style="font-weight:700">第{{ ch.no }}章 {{ ch.title }}（{{ ch.word_count }}字）</span>
          <div>
            <el-tag :type="ch.review_status === 'machine_hit' ? 'danger' : 'warning'" style="margin-right:10px">
              {{ ch.review_status }}
            </el-tag>
            <el-button type="success" :loading="acting" @click="act('approve')">通过</el-button>
            <el-button type="danger" :loading="acting" @click="act('reject')">驳回</el-button>
            <el-button @click="$router.back()">返回</el-button>
          </div>
        </div>
      </template>

      <el-alert v-if="ch.label" type="warning" :closable="false" style="margin-bottom:14px"
                :title="`机审标签：${ch.label}`" />
      <el-alert v-if="ch.conflicts && ch.conflicts.length" type="error" :closable="false"
                style="margin-bottom:14px"
                :title="`一致性冲突 ${ch.conflicts.length} 处`"
                :description="ch.conflicts.join('；')" />

      <el-card shadow="never" style="margin-bottom:14px;background:#fafafa">
        <template #header><span style="font-size:14px;color:#666">审核记录</span></template>
        <el-timeline v-if="ch.records && ch.records.length">
          <el-timeline-item v-for="(r, i) in ch.records" :key="i" :timestamp="r.created_at">
            [{{ r.stage }}] {{ r.action }} {{ r.label }}
          </el-timeline-item>
        </el-timeline>
        <span v-else style="color:#999">暂无记录</span>
      </el-card>

      <div class="content">{{ ch.content }}</div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const route = useRoute(), router = useRouter()
const ch = ref(null), loading = ref(false), acting = ref(false)

async function load() {
  loading.value = true
  try { ch.value = await api.get('/chapters/' + route.params.id) } finally { loading.value = false }
}

async function act(action) {
  if (action === 'reject') {
    await ElMessageBox.confirm('驳回后该章节需重新生成，确认？', '驳回确认', { type: 'warning' })
  }
  acting.value = true
  try {
    await api.post(`/chapters/${route.params.id}/review`, { action })
    ElMessage.success(action === 'approve' ? '已通过' : '已驳回')
    router.push('/review')
  } finally { acting.value = false }
}
onMounted(load)
</script>

<style scoped>
.content { white-space: pre-wrap; line-height: 2; font-size: 15px; max-height: 60vh; overflow-y: auto; }
</style>
