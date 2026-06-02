package com.callblocker.app.data.db

import androidx.lifecycle.LiveData
import androidx.room.*

@Dao
interface CallLogDao {
    @Query("SELECT * FROM call_log ORDER BY blockedAt DESC")
    fun getAll(): LiveData<List<CallLogEntry>>

    @Insert
    suspend fun insert(entry: CallLogEntry)

    @Query("DELETE FROM call_log")
    suspend fun deleteAll()
}
