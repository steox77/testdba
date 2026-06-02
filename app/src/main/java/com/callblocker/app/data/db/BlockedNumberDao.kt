package com.callblocker.app.data.db

import androidx.lifecycle.LiveData
import androidx.room.*

@Dao
interface BlockedNumberDao {
    @Query("SELECT * FROM blocked_numbers ORDER BY addedAt DESC")
    fun getAll(): LiveData<List<BlockedNumber>>

    @Query("SELECT * FROM blocked_numbers WHERE phoneNumber = :number LIMIT 1")
    suspend fun findByNumber(number: String): BlockedNumber?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insert(number: BlockedNumber)

    @Delete
    suspend fun delete(number: BlockedNumber)

    @Query("SELECT COUNT(*) FROM blocked_numbers WHERE phoneNumber = :number")
    suspend fun countByNumber(number: String): Int
}
