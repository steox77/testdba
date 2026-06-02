package com.callblocker.app.ui.log

import android.view.LayoutInflater
import android.view.ViewGroup
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.callblocker.app.data.db.CallLogEntry
import com.callblocker.app.databinding.ItemCallLogBinding
import java.text.SimpleDateFormat
import java.util.*

class CallLogAdapter : ListAdapter<CallLogEntry, CallLogAdapter.ViewHolder>(DIFF_CALLBACK) {

    private val dateFormat = SimpleDateFormat("dd/MM/yyyy HH:mm", Locale.ITALY)

    inner class ViewHolder(private val binding: ItemCallLogBinding) :
        RecyclerView.ViewHolder(binding.root) {

        fun bind(item: CallLogEntry) {
            binding.tvNumber.text = item.phoneNumber
            binding.tvReason.text = item.reason
            binding.tvDate.text = dateFormat.format(Date(item.blockedAt))
        }
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ViewHolder {
        val binding = ItemCallLogBinding.inflate(
            LayoutInflater.from(parent.context), parent, false
        )
        return ViewHolder(binding)
    }

    override fun onBindViewHolder(holder: ViewHolder, position: Int) {
        holder.bind(getItem(position))
    }

    companion object {
        private val DIFF_CALLBACK = object : DiffUtil.ItemCallback<CallLogEntry>() {
            override fun areItemsTheSame(a: CallLogEntry, b: CallLogEntry) = a.id == b.id
            override fun areContentsTheSame(a: CallLogEntry, b: CallLogEntry) = a == b
        }
    }
}
