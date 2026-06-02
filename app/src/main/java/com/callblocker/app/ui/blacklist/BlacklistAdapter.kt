package com.callblocker.app.ui.blacklist

import android.view.LayoutInflater
import android.view.ViewGroup
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.callblocker.app.data.db.BlockedNumber
import com.callblocker.app.databinding.ItemBlockedNumberBinding

class BlacklistAdapter(
    private val onDelete: (BlockedNumber) -> Unit
) : ListAdapter<BlockedNumber, BlacklistAdapter.ViewHolder>(DIFF_CALLBACK) {

    inner class ViewHolder(private val binding: ItemBlockedNumberBinding) :
        RecyclerView.ViewHolder(binding.root) {

        fun bind(item: BlockedNumber) {
            binding.tvNumber.text = item.phoneNumber
            binding.tvLabel.text = item.label.ifBlank { "Nessuna etichetta" }
            binding.btnDelete.setOnClickListener { onDelete(item) }
        }
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ViewHolder {
        val binding = ItemBlockedNumberBinding.inflate(
            LayoutInflater.from(parent.context), parent, false
        )
        return ViewHolder(binding)
    }

    override fun onBindViewHolder(holder: ViewHolder, position: Int) {
        holder.bind(getItem(position))
    }

    companion object {
        private val DIFF_CALLBACK = object : DiffUtil.ItemCallback<BlockedNumber>() {
            override fun areItemsTheSame(a: BlockedNumber, b: BlockedNumber) = a.id == b.id
            override fun areContentsTheSame(a: BlockedNumber, b: BlockedNumber) = a == b
        }
    }
}
