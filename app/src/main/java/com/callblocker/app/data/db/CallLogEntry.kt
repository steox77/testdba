package com.callblocker.app.data.db

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "call_log")
data class CallLogEntry(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val phoneNumber: String,
    val blockedAt: Long = System.currentTimeMillis(),
    val reason: String = "Lista nera"
)
