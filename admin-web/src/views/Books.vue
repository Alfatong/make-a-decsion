<template>
  <el-card>
    <template #header>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-weight:700">书籍管理</span>
        <el-button size="small" @click="load">刷新</el-button>
      </div>
    </template>
    <el-table :data="rows" v-loading="loading">
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column prop="title" label="书名" />
      <el-table-column label="章节进度" width="120">
        <template #default="{ row }">{{ row.chapters }}/{{ row.total_chapters }}</template>
      </el-table-column>
      <el-table-column prop="status" label="状态" width="110">
        <template #default="{ row }">
          <el-tag :type="{ on_shelf: 'success', off_shelf: 'info', draft: 'warning' }[row.status] || 'warning'">
            {{ { on_shelf: '已上架', off_shelf: '已下架', draft: '草稿', generating: '生成中', reviewing: '审核中' }[row.status] || row.status }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="180">
        <template #default="{ row }">
          <el-button v-if="row.status !== 'on_shelf'" type="success" size="small"
                     :loading="acting" @click="shelf(row, true)">上架</el-button>
          <el-button v-else type="warning" size="small"
                     :loading="acting" @click="shelf(row, false)">下架</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'

const rows = ref([]), loading = ref(false), acting = ref(false)

async function load() {
  loading.value = true
  try { rows.value = await api.get('/books') } finally { loading.value = false }
}
async function shelf(row, on) {
  acting.value = true
  try {
    await api.post(`/books/${row.id}/${on ? 'shelf' : 'off-shelf'}`)
    ElMessage.success(on ? '已上架' : '已下架'); load()
  } finally { acting.value = false }
}
onMounted(load)
</script>
