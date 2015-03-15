// Copyright 2014, Additive Regularization of Topic Models.

#include "artm/core/generation.h"

#include "boost/lexical_cast.hpp"
#include "boost/uuid/uuid_io.hpp"
#include "boost/uuid/uuid_generators.hpp"  // generators

#include "glog/logging.h"

#include "artm/core/common.h"
#include "artm/core/helpers.h"
#include "artm/core/exceptions.h"

namespace artm {
namespace core {

DiskGeneration::DiskGeneration(const std::string& disk_path)
    : disk_path_(disk_path), generation_() {
  std::vector<BatchManagerTask> batches = BatchHelpers::ListAllBatches(disk_path);
  for (int i = 0; i < batches.size(); ++i) {
    generation_.push_back(batches[i]);
  }
}

boost::uuids::uuid DiskGeneration::AddBatch(const std::shared_ptr<Batch>& batch) {
  std::string message = "ArtmAddBatch() is not allowed with current configuration. ";
  message += "Please, set the configuration parameter MasterComponentConfig.disk_path ";
  message += "to an empty string in order to enable ArtmAddBatch() operation. ";
  message += "Use ArtmSaveBatch() operation to save batches to disk.";
  BOOST_THROW_EXCEPTION(InvalidOperation(message));
}

void DiskGeneration::RemoveBatch(const boost::uuids::uuid& uuid) {
  LOG(ERROR) << "Remove batch is not supported in disk generation.";
}

std::vector<BatchManagerTask> DiskGeneration::batch_uuids() const {
  return generation_;
}

std::shared_ptr<Batch> DiskGeneration::batch(const BatchManagerTask& task) {
  auto batch = std::make_shared< ::artm::Batch>();
  ::artm::core::BatchHelpers::LoadMessage(task.file_path, batch.get());
  batch->set_id(boost::lexical_cast<std::string>(task.uuid));  // keep batch.id and task.uuid in sync
  ::artm::core::BatchHelpers::PopulateClassId(batch.get());
  return batch;
}

}  // namespace core
}  // namespace artm

