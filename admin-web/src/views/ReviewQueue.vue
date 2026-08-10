<template>
  <el-card>
    <template #header>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-weight:700">待复核队列</span>
        <el-button size="small" @click="load">刷新</el-button>
      </div>
    </template>
    <el-table :data="rows" v-loading="loading" empty-text="暂无待复核章节">
      <el-table-column prop="book_title" label="作品" width="160" />
      <el-table-column prop="no" label="章节" width="80">
        <template #default="{ row }">第{{ row.no }}章</template>
      </el-table-column>
      <el-table-column prop="title" label="标题" show-overflow-tooltip />
      <el-table-column prop="review_status" label="状态" width="110">
        <template #default="{ row }">
          <el-tag :type="row.review_status === 'machine_hit' ? 'danger' : 'warning'">
            {{ row.review_status === 'machine_hit' ? '机审命中' : '待处理' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="label" label="机审标签" width="100" />
      <el-table-column label="一致性冲突" width="110">
        <template #default="{ row }">
          <el-tag v-if="row.conflicts && row.conflicts.length" type="warning">
            {{ row.conflicts.length }} 处
          </el-tag>
          <span v-else style="color:#999">无</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="120">
        <template #default="{ row }">
          <el-button type="primary" size="small" @click="$router.push('/review/' + row.chapter_id)">
            去复核
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import api from '../api'

const rows = ref([]), loading = ref(false)
async function load() {
  loading.value = true
  try { rows.value = await api.get('/review-queue') } finally { loading.value = false }
}
onMounted(load)
</script>
