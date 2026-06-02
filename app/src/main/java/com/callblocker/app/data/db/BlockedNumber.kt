package com.callblocker.app.data.db

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "blocked_numbers")
data class BlockedNumber(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val phoneNumber: String,
    val label: String = "",
    val addedAt: Long = System.currentTimeMillis()
)
